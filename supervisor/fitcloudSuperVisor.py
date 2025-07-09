import os
import boto3
import logging
from http import HTTPStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT1_ID = os.environ.get("AGENT1_ID", "NBLVKZOU76")
AGENT2_ID = os.environ.get("AGENT2_ID", "7HPRF6E9UD")
AGENT2_KEYWORDS = ["보고서", "리포트", "엑셀", "차트", "그래프", "PDF", "파일", "첨부", "다운로드", "업로드", "슬랙"]

def lambda_handler(event, context):
    import json
    logger.info(f"event 구조: {json.dumps(event, ensure_ascii=False)}")
    while isinstance(event, list):
        event = event[0]

    # user_input 추출 로직 (event 구조에 따라 분기)
    user_input = None
    try:
        # 1. parameters가 dict로 들어오는 경우
        if isinstance(event.get("parameters"), dict):
            user_input = event["parameters"].get("user_input")
        # 2. parameters가 list이거나 없을 때, requestBody에서 추출
        if not user_input:
            props = event["requestBody"]["content"]["application/json"]["properties"]
            for prop in props:
                if prop.get("name") == "user_input":
                    user_input = prop.get("value")
                    break
    except Exception as e:
        logger.error(f"user_input 추출 실패: {e}")

    if not user_input:
        logger.error("[Agent0] user_input 파라미터가 없습니다.")
        return {
            'statusCode': HTTPStatus.BAD_REQUEST,
            'body': 'user_input 파라미터가 필요합니다.'
        }

    try:
        # 분기: Agent2 키워드 포함 여부
        if any(keyword in user_input for keyword in AGENT2_KEYWORDS):
            target_agent_id = AGENT2_ID
            logger.info(f"[Agent0] Agent2({target_agent_id})로 위임 시작, user_input: {user_input}")
        else:
            target_agent_id = AGENT1_ID
            logger.info(f"[Agent0] Agent1({target_agent_id})로 위임 시작, user_input: {user_input}")

        # Bedrock Agent Runtime 호출
        client = boto3.client("bedrock-agent-runtime")
        response = client.invoke_agent(
            agentId=target_agent_id,
            sessionId="your-session-id",  # 필요시 고유 세션ID 생성/전달
            inputText=user_input
        )
        result = response.get("completion", "")
        logger.info(f"[Agent0] {target_agent_id} 응답: {result}")

        return {
            'statusCode': 200,
            'body': result
        }

    except Exception as e:
        logger.error(f"[Agent0] 에이전트 호출 중 오류: {e}", exc_info=True)
        return {
            'statusCode': HTTPStatus.INTERNAL_SERVER_ERROR,
            'body': f'Agent0에서 에이전트 호출 중 오류: {str(e)}'
        } 