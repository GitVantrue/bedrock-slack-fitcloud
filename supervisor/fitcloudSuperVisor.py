import os
import boto3
import logging
from http import HTTPStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT1_ID = os.environ.get("AGENT1_ID", "7HPRF6E9UD")
AGENT1_ALIAS = os.environ.get("AGENT1_ALIAS", "Z6NLZGHRTE")
AGENT2_ID = os.environ.get("AGENT2_ID", "NBLVKZOU76")
AGENT2_ALIAS = os.environ.get("AGENT2_ALIAS", "PSADGJ398L")
AGENT2_KEYWORDS = ["보고서", "리포트", "엑셀", "차트", "그래프", "PDF", "파일", "첨부", "다운로드", "업로드", "슬랙"]

def lambda_handler(event, context):
    import json
    logger.info(f"event 구조: {json.dumps(event, ensure_ascii=False)}")
    while isinstance(event, list):
        event = event[0]

    # user_input 추출 로직 (event 구조에 따라 분기)
    user_input = None
    try:
        # 1. 직접 user_input이 있는 경우
        if isinstance(event, dict) and "user_input" in event:
            user_input = event["user_input"]
        # 2. parameters가 dict로 들어오는 경우
        elif isinstance(event.get("parameters"), dict):
            user_input = event["parameters"].get("user_input")
        # 3. parameters가 list이거나 없을 때, requestBody에서 추출
        elif event.get("requestBody") and event["requestBody"].get("content"):
            try:
                props = event["requestBody"]["content"]["application/json"]["properties"]
                for prop in props:
                    if prop.get("name") == "user_input":
                        user_input = prop.get("value")
                        break
            except Exception as e:
                logger.error(f"requestBody에서 user_input 추출 실패: {e}")
        # 4. inputText가 있는 경우
        elif event.get("inputText"):
            user_input = event["inputText"]
    except Exception as e:
        logger.error(f"user_input 추출 실패: {e}")

    if not user_input:
        logger.error("[Agent0] user_input 파라미터가 없습니다.")
        return {
            'statusCode': HTTPStatus.BAD_REQUEST,
            'body': json.dumps({
                'message': 'user_input 파라미터가 필요합니다.'
            }, ensure_ascii=False)
        }

    try:
        # 분기: Agent2 키워드 포함 여부
        if any(keyword in user_input for keyword in AGENT2_KEYWORDS):
            target_agent_id = AGENT2_ID
            target_agent_alias = AGENT2_ALIAS
            logger.info(f"[Agent0] Agent2({target_agent_id})로 위임 시작, user_input: {user_input}")
        else:
            target_agent_id = AGENT1_ID
            target_agent_alias = AGENT1_ALIAS
            logger.info(f"[Agent0] Agent1({target_agent_id})로 위임 시작, user_input: {user_input}")

        # Bedrock Agent Runtime 호출
        client = boto3.client("bedrock-agent-runtime")
        # Agent1 먼저 호출해서 sessionAttributes 확보 (Agent2 키워드일 때만)
        session_attributes = None
        conversation_history = []
        
        if target_agent_id == AGENT2_ID:
            # Agent1 호출
            agent1_response = client.invoke_agent(
                agentId=AGENT1_ID,
                agentAliasId=AGENT1_ALIAS,
                sessionId="your-session-id",
                inputText=user_input
            )
            agent1_result = ""
            agent1_response_data = None
            
            try:
                # EventStream 객체 처리
                for event in agent1_response:
                    if 'chunk' in event and 'bytes' in event['chunk']:
                        chunk_data = event['chunk']['bytes'].decode('utf-8')
                        agent1_result += chunk_data
                        
                        # JSON 응답 구조 파싱 시도
                        try:
                            if chunk_data.strip().startswith('{'):
                                parsed_chunk = json.loads(chunk_data)
                                if 'response' in parsed_chunk or 'body' in parsed_chunk:
                                    agent1_response_data = parsed_chunk
                        except json.JSONDecodeError:
                            pass  # JSON이 아닌 경우 무시
                            
            except Exception as e:
                logger.error(f"Agent1 EventStream 파싱 실패: {e}")
                agent1_result = f"Agent1 호출 실패: {str(e)}"
            
            # conversationHistory 구성
            conversation_history = {
                "messages": [
                    {
                        "role": "user",
                        "content": user_input
                    },
                    {
                        "role": "assistant", 
                        "content": agent1_result
                    }
                ]
            }
            
            # sessionAttributes 추출 및 개선
            if hasattr(agent1_response, 'get'):
                session_attributes = agent1_response.get("sessionAttributes", {})
            else:
                session_attributes = {}
            
            # Agent1 결과를 sessionAttributes에 저장 (Agent2가 활용할 수 있도록)
            if agent1_result:
                session_attributes["last_cost_message"] = str(agent1_result)
                
                # Agent1 응답 데이터 저장 (우선순위: 파싱된 JSON > 전체 텍스트)
                if agent1_response_data:
                    session_attributes["agent1_response_data"] = json.dumps(agent1_response_data, ensure_ascii=False)
                    logger.info(f"[Agent0] Agent1 JSON 응답 저장 완료")
                else:
                    # JSON 파싱 실패 시 전체 응답을 저장
                    session_attributes["agent1_response_data"] = json.dumps({"body": agent1_result}, ensure_ascii=False)
                    logger.info(f"[Agent0] Agent1 텍스트 응답 저장 완료")
                
                session_attributes["agent1_response_processed"] = "true"
                
                # Agent1에서 표 데이터가 있다면 그것도 저장
                if "표" in agent1_result or "데이터" in agent1_result:
                    session_attributes["last_cost_table"] = str(agent1_result)
        
        # Agent2 호출 시 sessionState 전달 (Agent1 응답 포함)
        agent2_kwargs = dict(
            agentId=target_agent_id,
            agentAliasId=target_agent_alias,
            sessionId="your-session-id",
            inputText=user_input
        )
        
        # sessionState 구성 (conversationHistory 포함)
        session_state = {
            "sessionAttributes": session_attributes or {}
        }
        
        # conversationHistory가 있으면 추가
        if conversation_history:
            session_state["conversationHistory"] = conversation_history
            logger.info(f"[Agent0] conversationHistory 추가: {len(conversation_history)}개 메시지")
        
        agent2_kwargs["sessionState"] = session_state
        
        try:
            response = client.invoke_agent(**agent2_kwargs)
            # EventStream 객체 직접 파싱
            result = ""
            try:
                for event in response:
                    if 'chunk' in event and 'bytes' in event['chunk']:
                        result += event['chunk']['bytes'].decode('utf-8')
            except Exception as e:
                logger.error(f"EventStream 파싱 실패: {e}")
                result = f"[Agent0] EventStream 파싱 실패: {str(e)}"
            
            # result가 비어 있으면 에러 메시지로 대체
            if not result:
                result = "[Agent0] Bedrock Agent 응답 파싱 실패 또는 빈 응답"
                
        except Exception as e:
            logger.error(f"Agent 호출 실패: {e}")
            result = f"[Agent0] Agent 호출 실패: {str(e)}"
        
        # EventStream 객체를 직접 로깅하지 않고 결과만 로깅
        logger.info(f"[Agent0] {target_agent_id} 응답 완료, 결과 길이: {len(result) if result else 0}")
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': result
            }, ensure_ascii=False)
        }

    except Exception as e:
        logger.error(f"[Agent0] 에이전트 호출 중 오류: {e}", exc_info=True)
        return {
            'statusCode': HTTPStatus.INTERNAL_SERVER_ERROR,
            'body': json.dumps({
                'message': f'Agent0에서 에이전트 호출 중 오류: {str(e)}'
            }, ensure_ascii=False)
        } 