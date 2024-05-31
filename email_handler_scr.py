import os
import base64
import time
import psutil
from openai import OpenAI
from email import message_from_bytes, policy
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.mime.text import MIMEText
import logging
from dotenv import load_dotenv
from agent import Agent
from search import WebSearcher
from prompts import planning_agent_prompt, integration_agent_prompt

# Configure logging
logging.basicConfig(level=logging.INFO, filename="script.log", filemode='a', format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Access the OpenAI API Key from environment variables
api_key = os.getenv('OPENAI_API_KEY')
if not api_key:
    raise Exception("OPENAI_API_KEY environment variable not set")

client = OpenAI(api_key=api_key)

# SCOPES for Gmail API
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.modify'
]

def authenticate_gmail():
    try:
        creds = None
        token_path = 'token.json'
        credentials_path = 'credentials.json'
        
        # Load the token if it exists
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        
        # If there are no valid credentials available, request new ones
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    # Handle token expiration and re-authentication
                    logger.warning('Token expired or revoked, need to re-authenticate.')
                    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                    creds = flow.run_local_server(port=0)
                    # Save the new credentials for the next run
                    with open(token_path, 'w') as token:
                        token.write(creds.to_json())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
                # Save the new credentials for the next run
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())

        service = build('gmail', 'v1', credentials=creds)
        return service
    except Exception as e:
        logger.error(f'Authentication failed: {e}')
        return None

def fetch_unread_emails(service):
    try:
        results = service.users().messages().list(userId='me', labelIds=['INBOX'], q='is:unread').execute()
        return results.get('messages', [])
    except HttpError as error:
        logger.error(f'An error occurred: {error}')
        return []

def get_email_content(service, msg_id):
    try:
        message = service.users().messages().get(userId='me', id=msg_id, format='raw').execute()
        msg_str = base64.urlsafe_b64decode(message['raw'].encode('ASCII'))
        mime_msg = message_from_bytes(msg_str, policy=policy.default)

        if mime_msg.is_multipart():
            for part in mime_msg.iter_parts():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                if "attachment" not in content_disposition:
                    if content_type == "text/plain":
                        return part.get_payload(decode=True).decode(), mime_msg
                    elif content_type == "text/html":
                        html = part.get_payload(decode=True).decode()
                        soup = BeautifulSoup(html, 'html.parser')
                        return soup.get_text(separator='\n', strip=True), mime_msg
        else:
            payload = mime_msg.get_payload(decode=True)
            if payload:
                return payload.decode(), mime_msg

        logger.error(f'No suitable payload found for email ID: {msg_id}')
        return None, None
    except HttpError as error:
        logger.error(f'An error occurred: {error}')
        return None, None
    except Exception as e:
        logger.error(f'Unexpected error: {e}')
        return None, None

def generate_response(email_content):
    web_searcher = WebSearcher(model="gpt-4o")
    agent = Agent(
        model="gpt-4o",
        tool=web_searcher,
        planning_agent_prompt=planning_agent_prompt,
        integration_agent_prompt=integration_agent_prompt,
        verbose=True
    )
    plan = None
    outputs = None
    response = None
    meets_requirements = False
    iterations = 0

    while not meets_requirements and iterations < 3:
        iterations += 1
        plan = agent.run_planning_agent(email_content, plan=plan, outputs=outputs, feedback=response)
        outputs = agent.tool.use_tool(plan=plan, query=email_content)
        response = agent.run_integration_agent(email_content, plan, outputs)
        meets_requirements = agent.check_response(response, email_content)

    return response

def send_email(service, to, subject, body):
    try:
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        message = {'raw': raw}
        sent_message = service.users().messages().send(userId='me', body=message).execute()
        logger.info(f'Message Id: {sent_message["id"]}')
    except HttpError as error:
        logger.error(f'Error sending email: {error}')
    except ValueError as ve:
        logger.error(f'Value error: {ve}')

def mark_as_read(service, msg_id):
    try:
        logger.info(f'Marking email {msg_id} as read...')
        service.users().messages().modify(userId='me', id=msg_id, body={'removeLabelIds': ['UNREAD']}).execute()
        logger.info(f'Marked email {msg_id} as read.')
    except HttpError as error:
        logger.error(f'Error marking email as read: {error}')

def process_email(service, msg_id):
    email_content, headers = get_email_content(service, msg_id)
    if email_content:
        logger.info(f'Email content: {email_content}')
        logger.info(f'Email headers: {headers}')
        response = generate_response(email_content)
        logger.info(f'Generated response: {response}')

        # Ensure we have the From header
        to_email = headers.get('From')
        if not to_email:
            logger.error(f'Missing "From" header for email ID: {msg_id}')
            return

        subject = f"Re: {headers.get('Subject', 'No Subject')}"
        send_email(service, to_email, subject, response)
        logger.info(f'Responded to email from {to_email}')
        mark_as_read(service, msg_id)

def monitor_resources():
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        memory_info = psutil.virtual_memory()
        memory_usage = memory_info.percent

        logger.info(f"CPU Usage: {cpu_usage}%")
        logger.info(f"Memory Usage: {memory_usage}%")

        return cpu_usage, memory_usage
    except Exception as e:
        logger.error(f'Error monitoring resources: {e}')
        return 0, 0

def main():
    service = authenticate_gmail()
    if not service:
        return

    while True:
        try:
            messages = fetch_unread_emails(service)
            if not messages:
                logger.info('No new messages.')
            else:
                for message in messages:
                    process_email(service, message['id'])

            # Monitor resources and adjust sleep interval
            cpu_usage, memory_usage = monitor_resources()
            if cpu_usage > 60 or memory_usage > 70:
                logger.warning("High resource usage detected. Sleeping for an extended period.")
                time.sleep(1200)  # Sleep for 20 minutes if high resource usage
            else:
                time.sleep(300)  # Sleep for 5 minutes otherwise
        except Exception as e:
            logger.error(f'An error occurred in the main loop: {e}')
            time.sleep(300)  # Sleep for 5 minutes before retrying

if __name__ == '__main__':
    main()