import os
import boto3
import logging
import json

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT1_ID = os.environ.get("AGENT1_ID", "7HPRF6E9UD")
AGENT1_ALIAS = os.environ.get("AGENT1_ALIAS", "Z6NLZGHRTE")
AGENT2_ID = os.environ.get("AGENT2_ID", "NBLVKZOU76")
AGENT2_ALIAS = os.environ.get("AGENT2_ALIAS", "PSADGJ398L")
AGENT2_KEYWORDS = ["보고서", "리포트", "엑셀", "차트", "그래프", "PDF", "파일", "첨부", "다운로드", "업로드", "슬랙", "만들어", "생성", "제작"]

def lambda_handler(event, context):
    logger.info(f"[Supervisor] Raw event: {json.dumps(event, ensure_ascii=False)[:1000]}")
    # user_input 추출
    user_input = None
    try:
        if isinstance(event, dict) and "user_input" in event:
            user_input = event["user_input"]
        elif isinstance(event.get("parameters"), dict):
            user_input = event["parameters"].get("user_input")
        elif event.get("requestBody") and event["requestBody"].get("content"):
            try:
                props = event["requestBody"]["content"]["application/json"]["properties"]
                for prop in props:
                    if prop.get("name") == "user_input":
                        user_input = prop.get("value")
                        break
            except Exception as e:
                logger.error(f"requestBody에서 user_input 추출 실패: {e}")
        elif event.get("inputText"):
            user_input = event["inputText"]
    except Exception as e:
        logger.error(f"user_input 추출 실패: {e}")
    if not user_input:
        logger.error("[Supervisor] user_input 파라미터가 없습니다.")
        return {
            'response': {
                'body': {
                    'content': [
                        {
                            'type': 'text',
                            'text': 'user_input 파라미터가 필요합니다.'
                        }
                    ]
                }
            }
        }
    # Bedrock Agent Runtime 클라이언트
    client = boto3.client("bedrock-agent-runtime")
    session_id = f"supervisor-session"
    # 1. Agent1 직접 호출
    logger.info(f"[Supervisor] Agent1({AGENT1_ID}) 호출 시작")
    agent1_response = client.invoke_agent(
        agentId=AGENT1_ID,
        agentAliasId=AGENT1_ALIAS,
        sessionId=session_id,
        inputText=user_input
    )
    agent1_result_text = ""
    try:
        for event in agent1_response:
            if 'chunk' in event and 'bytes' in event['chunk']:
                agent1_result_text += event['chunk']['bytes'].decode('utf-8')
    except Exception as e:
        logger.error(f"Agent1 EventStream 파싱 실패: {e}")
        agent1_result_text = f"Agent1 호출 실패: {str(e)}"
    if not agent1_result_text:
        agent1_result_text = "[Supervisor] Agent1으로부터 유효한 응답을 받지 못했습니다."
    logger.info(f"[Supervisor] Agent1 최종 응답: {agent1_result_text[:300]}")
    # 2. Agent2 호출 (inputText에 Agent1 결과 명시적 포함)
    agent2_input_text = f"보고서를 만들어주세요. 조회된 데이터:\n{agent1_result_text}"
    logger.info(f"[Supervisor] Agent2 호출용 inputText: {agent2_input_text[:300]}")
    agent2_response = client.invoke_agent(
        agentId=AGENT2_ID,
        agentAliasId=AGENT2_ALIAS,
        sessionId=session_id,
        inputText=agent2_input_text,
        sessionState={
            "sessionAttributes": {}
        }
    )
    agent2_result = ""
    try:
        for event in agent2_response:
            if 'chunk' in event and 'bytes' in event['chunk']:
                agent2_result += event['chunk']['bytes'].decode('utf-8')
    except Exception as e:
        logger.error(f"Agent2 EventStream 파싱 실패: {e}")
        agent2_result = f"Agent2 호출 실패: {str(e)}"
    if not agent2_result:
        agent2_result = "[Supervisor] Agent2로부터 유효한 응답을 받지 못했습니다."
    logger.info(f"[Supervisor] Agent2 최종 응답: {agent2_result[:300]}")
    # 최종 응답 반환
    return {
        'response': {
            'body': {
                'content': [
                    {
                        'type': 'text',
                        'text': agent2_result
                    }
                ]
            }
        }
    } 