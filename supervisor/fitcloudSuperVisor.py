import os
import boto3
import logging
import json
import re

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
    # 세션 ID 추출 (Bedrock Agent의 세션 ID 사용)
    session_id = event.get("sessionId", "default-supervisor-session-fallback")
    logger.info(f"[Supervisor] 현재 세션 ID: {session_id}")
    # Bedrock Agent Runtime 클라이언트
    client = boto3.client("bedrock-agent-runtime")
    # 1. Agent1 직접 호출
    logger.info(f"[Supervisor] Agent1({AGENT1_ID}) 호출 시작")
    agent1_response = client.invoke_agent(
        agentId=AGENT1_ID,
        agentAliasId=AGENT1_ALIAS,
        sessionId=session_id,
        inputText=user_input
    )
    # 1. Agent1 응답 chunk 이어붙이기
    raw_agent1_response = ""
    for event in agent1_response:
        if 'chunk' in event and 'bytes' in event['chunk']:
            raw_agent1_response += event['chunk']['bytes'].decode('utf-8')
    # 2. 마크다운 텍스트만 추출 (JSON 파싱 우선)
    agent1_result_text = extract_markdown_from_agent1(raw_agent1_response)
    # 3. Agent2 호출
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

def extract_markdown_from_agent1(raw_response: str) -> str:
    try:
        # 1. JSON 파싱 시도
        parsed_json = json.loads(raw_response)
        if 'output' in parsed_json and \
           'message' in parsed_json['output'] and \
           'content' in parsed_json['output']['message'] and \
           len(parsed_json['output']['message']['content']) > 0 and \
           'text' in parsed_json['output']['message']['content'][0]:
            logger.info("[Supervisor] Agent1 JSON 응답에서 텍스트 추출 성공.")
            return parsed_json['output']['message']['content'][0]['text'].strip()
        logger.warning(f"[Supervisor] Agent1 JSON 응답이지만 예상 경로에서 텍스트를 찾을 수 없음. 원본: {raw_response[:200]}")
        return raw_response.strip()
    except json.JSONDecodeError:
        logger.info("[Supervisor] Agent1 응답이 JSON 형식이 아님. 정규식 추출 시도.")
        match = re.search(r"\[RESPONSE\]\[message\](.*)", raw_response, re.DOTALL)
        if match:
            logger.info("[Supervisor] [RESPONSE][message] 패턴으로 텍스트 추출 성공.")
            return match.group(1).strip()
        md_match = re.search(r"(\*━━━━━━━━+.*?)(?:END RequestId|$)", raw_response, re.DOTALL)
        if md_match:
            logger.info("[Supervisor] 마크다운 패턴으로 텍스트 추출 성공.")
            return md_match.group(1).strip()
        logger.warning("[Supervisor] 특정 패턴으로 텍스트 추출 실패. Agent1 원본 응답 반환.")
        return raw_response.strip() 