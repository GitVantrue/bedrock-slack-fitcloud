import json
import os
import hmac
import hashlib
import time
import requests
import logging
import boto3
import urllib.parse
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# --- Secrets Manager 클라이언트 및 시크릿 가져오는 함수 ---
secrets_manager = boto3.client('secretsmanager')
SLACK_SECRETS_NAME = os.environ.get('SLACK_SECRETS_NAME', 'SlackAppCredentials')
SLACK_BOT_TOKEN_KEY = os.environ.get('SLACK_BOT_TOKEN_KEY', 'slackBotToken')
SLACK_SIGNING_SECRET_KEY = os.environ.get('SLACK_SIGNING_SECRET_KEY', 'slackSigningSecret')

cached_secrets = None

def get_slack_secrets():
    global cached_secrets
    if cached_secrets is not None:
        return cached_secrets

    try:
        response = secrets_manager.get_secret_value(SecretId=SLACK_SECRETS_NAME)
        secret_string = response['SecretString']
        secrets = json.loads(secret_string)
        cached_secrets = secrets
        return secrets
    except Exception as e:
        logger.error(f"Error retrieving Slack secrets from Secrets Manager: {e}", exc_info=True)
        raise e

# --- Slack 요청 서명 검증 함수 (보안 필수) ---
def verify_slack_request(headers, body):
    try:
        slack_secrets = get_slack_secrets()
        slack_signing_secret = slack_secrets.get('slackSigningSecret')

        if not slack_signing_secret:
            logger.error("Slack Signing Secret not found in Secrets Manager. Check secret name and key.")
            return False

        # 헤더 이름을 소문자로 가져옵니다.
        slack_signature = headers.get('x-slack-signature')
        slack_timestamp = headers.get('x-slack-request-timestamp')

        if not slack_signature or not slack_timestamp:
            logger.error("Missing Slack signature or timestamp headers.")
            return False

        # 타임스탬프 유효성 검사 (5분 이내)
        if abs(time.time() - int(slack_timestamp)) > 60 * 5:
            logger.error(f"Timestamp verification failed. Request timestamp: {slack_timestamp}, Current time: {time.time()}")
            return False

        # 서명 베이스 스트링 생성
        basestring = f"v0:{slack_timestamp}:{body}".encode('utf-8')

        # HMAC-SHA256 서명 생성
        my_signature = 'v0=' + hmac.new(
            slack_signing_secret.encode('utf-8'),
            basestring,
            hashlib.sha256
        ).hexdigest()

        # 서명 비교
        if not hmac.compare_digest(my_signature, slack_signature):
            logger.error(f"Signature verification failed. My signature: {my_signature}, Slack signature: {slack_signature}")
            return False

        return True
    except Exception as e:
        logger.error(f"Error during Slack request verification: {e}", exc_info=True)
        return False

# --- Slack 메시지 전송 함수 (thread_ts 파라미터 제거) ---
def send_slack_message(channel, message):
    try:
        slack_secrets = get_slack_secrets()
        slack_bot_token = slack_secrets.get('slackBotToken')

        if not slack_bot_token:
            logger.error("Slack Bot Token not found in Secrets Manager. Cannot send message.")
            return

        slack_api_url = "https://slack.com/api/chat.postMessage"
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {slack_bot_token}'
        }
        payload = {
            'channel': channel,
            'text': message
        }
        # thread_ts 관련 코드 제거

        response = requests.post(slack_api_url, headers=headers, data=json.dumps(payload))
        response.raise_for_status() # HTTP 오류 발생 시 예외 발생
        response_json = response.json()
        if not response_json.get("ok"):
            logger.error(f"Failed to send Slack message: {response_json.get('error')}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending message to Slack: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error in send_slack_message: {e}", exc_info=True)

# --- Lambda 핸들러 함수 (메인 진입점) ---
def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    headers = event.get('headers', {})
    content_type = headers.get('content-type', '').lower()
    raw_body = event.get('body', '')

    # --- Slack 재시도 이벤트 처리: X-Slack-Retry-Num 헤더가 있으면 이미 처리된 이벤트일 가능성이 높으므로 무시 ---
    if 'x-slack-retry-num' in headers:
        logger.info(f"Ignoring Slack retry event: {headers.get('x-slack-retry-num')}")
        return {'statusCode': 200, 'body': 'OK'}

    # --- body_json_dict 초기화 및 Slack URL Verification/JSON 파싱 ---
    body_json_dict = {}
    if content_type == 'application/json' and raw_body:
        try:
            temp_body_dict = json.loads(raw_body)
            if 'challenge' in temp_body_dict:
                logger.info(f"Received Slack URL verification challenge: {temp_body_dict['challenge']}")
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'text/plain'},
                    'body': temp_body_dict['challenge']
                }
            else:
                body_json_dict = temp_body_dict
        except json.JSONDecodeError:
            logger.warning("Body is JSON but not a valid JSON string. Skipping JSON parsing for this path.")

    # --- 2. Slack Signature Verification (보안 필수) ---
    if not verify_slack_request(headers, raw_body):
        logger.warning("Slack signature verification failed. Rejecting request.")
        return {'statusCode': 403, 'body': 'Forbidden'}

    # --- 3. 실제 Slack 이벤트 처리 시작 ---
    if content_type == 'application/json':
        if not body_json_dict or 'event' not in body_json_dict:
            logger.warning("Received JSON request without 'event' key or invalid JSON. Ignoring.")
            return {'statusCode': 200, 'body': 'OK'}

        slack_event_payload = body_json_dict
        slack_event = slack_event_payload.get('event', {})

        # --- 봇 메시지 및 특정 이벤트 유형 무시 로직 강화 ---
        # 봇 본인이 보낸 메시지, Slackbot 메시지, 또는 사용자 메시지가 아닌 경우 무시
        if (slack_event.get('bot_id') or
            slack_event.get('subtype') == 'bot_message' or
            (slack_event.get('type') == 'message' and slack_event_payload.get('api_app_id') == slack_event.get('app_id')) or
            slack_event.get('user') == 'USLACKBOT' or
            (slack_event.get('type') == 'message' and 'client_msg_id' not in slack_event)
            ):
            logger.info("Ignoring bot message, app message, Slackbot message, or non-user message.")
            return {'statusCode': 200, 'body': 'OK'}

        event_type = slack_event.get('type')
        channel_id = slack_event.get('channel')
        user_id = slack_event.get('user')
        original_text = slack_event.get('text')

        if event_type == 'app_mention':
            bot_user_id = slack_event_payload.get('authed_users', [None])[0]
            if bot_user_id:
                text_for_agent = original_text.replace(f"<@{bot_user_id}>", "").strip()
            else:
                text_for_agent = original_text.strip()
        else:
            text_for_agent = original_text.strip()

        if not text_for_agent:
            response_message = f"안녕하세요, <@{user_id}>님! CI/CD 테스트 중입니다. 무엇을 도와드릴까요?"
            send_slack_message(channel_id, response_message)
            return {'statusCode': 200, 'body': 'OK'}

        try:
            # Bedrock Agent 런타임 클라이언트
            bedrock_agent_client = boto3.client('bedrock-agent-runtime')
            
            # TODO: 실제 Bedrock Agent ID와 Alias ID로 변경해야 합니다.
            # 이 값들을 환경 변수나 Secrets Manager에서 가져오는 것이 좋습니다.
            # 환경 변수에서 가져오려면 Lambda 함수 설정에 BEDROCK_AGENT_ID, BEDROCK_AGENT_ALIAS_ID를 추가해야 합니다.
            agent_id = os.environ.get('BEDROCK_AGENT_ID', 'YOUR_BEDROCK_AGENT_ID') # <-- 이 부분 변경!
            agent_alias_id = os.environ.get('BEDROCK_AGENT_ALIAS_ID', 'YOUR_BEDROCK_AGENT_ALIAS_ID') # <-- 이 부분 변경!

            # 세션 ID 생성 (대화 지속성을 위해 user_id와 channel_id를 조합)
            session_id = f"{user_id}-{channel_id}" 
           
            current_date_str = datetime.now().strftime('%Y년 %m월 %d일')
            current_year_str = str(datetime.now().year)

            logger.info(f"Invoking Bedrock Agent with text: '{text_for_agent}' for session: '{session_id}' "
                        f"with current_date: {current_date_str}, current_year: {current_year_str}")

            response = bedrock_agent_client.invoke_agent(
                agentId=agent_id,
                agentAliasId=agent_alias_id,
                sessionId=session_id,
                inputText=text_for_agent,
                # --- sessionAttributes 추가 ---
                sessionState={
                    'sessionAttributes': {
                        'current_date': current_date_str, # 현재 날짜 정보를 "current_date" 키로 전달
                        'current_year': current_year_str  # 현재 연도 정보를 "current_year" 키로 전달
                    }
                }
            )

            # 스트리밍 응답 처리
            agent_response_text = ""
            for chunk in response['completion']:
                if 'chunk' in chunk:
                    chunk_data = chunk['chunk']
                    if 'bytes' in chunk_data:
                        chunk_text = chunk_data['bytes'].decode('utf-8')
                        agent_response_text += chunk_text

            if agent_response_text.strip():
                send_slack_message(channel_id, agent_response_text.strip())
            else:
                send_slack_message(channel_id, "죄송합니다. 응답을 생성할 수 없습니다.")

        except Exception as e:
            logger.error(f"Error invoking Bedrock Agent: {e}", exc_info=True)
            error_message = f"죄송합니다, <@{user_id}>님. 처리 중 오류가 발생했습니다."
            send_slack_message(channel_id, error_message)

    return {'statusCode': 200, 'body': 'OK'}