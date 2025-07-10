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
AGENT2_KEYWORDS = ["보고서", "리포트", "엑셀", "차트", "그래프", "PDF", "파일", "첨부", "다운로드", "업로드", "슬랙", "만들어", "생성", "제작"]

def lambda_handler(event, context):
    import json
    logger.info(f"event 구조: {json.dumps(event, ensure_ascii=False)}")
    while isinstance(event, list):
        event = event[0]

    # === conversationHistory와 sessionAttributes 디버깅 로그 추가 ===
    logger.info(f"[DEBUG][Supervisor] conversationHistory 존재 여부: {'conversationHistory' in event}")
    if 'conversationHistory' in event:
        conversation_history = event['conversationHistory']
        logger.info(f"[DEBUG][Supervisor] conversationHistory 타입: {type(conversation_history)}")
        logger.info(f"[DEBUG][Supervisor] conversationHistory 내용: {json.dumps(conversation_history, ensure_ascii=False)[:500]}")
        if isinstance(conversation_history, dict) and 'messages' in conversation_history:
            logger.info(f"[DEBUG][Supervisor] conversationHistory 메시지 수: {len(conversation_history['messages'])}")
            for i, msg in enumerate(conversation_history['messages']):
                logger.info(f"[DEBUG][Supervisor] 메시지 {i}: role={msg.get('role')}, content 길이={len(str(msg.get('content', '')))}")
    else:
        logger.info(f"[DEBUG][Supervisor] conversationHistory가 event에 없습니다.")
    
    logger.info(f"[DEBUG][Supervisor] sessionAttributes 존재 여부: {'sessionAttributes' in event}")
    if 'sessionAttributes' in event:
        session_attrs = event['sessionAttributes']
        logger.info(f"[DEBUG][Supervisor] sessionAttributes 타입: {type(session_attrs)}")
        logger.info(f"[DEBUG][Supervisor] sessionAttributes 키 목록: {list(session_attrs.keys())}")
        logger.info(f"[DEBUG][Supervisor] sessionAttributes 내용: {json.dumps(session_attrs, ensure_ascii=False)[:500]}")
    else:
        logger.info(f"[DEBUG][Supervisor] sessionAttributes가 event에 없습니다.")

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

    try:
        # 세션 ID 생성 (영문/숫자만 사용)
        import re
        clean_input = re.sub(r'[^a-zA-Z0-9._:-]', '', user_input[:20])
        session_id = f"supervisor-{clean_input}" if clean_input else "supervisor-default"
        
        # 분기: 보고서 생성 요청인지 확인
        is_report_request = any(keyword in user_input for keyword in AGENT2_KEYWORDS)
        
        if is_report_request:
            # 보고서 생성 요청: Agent1 → Agent2 순서로 호출
            logger.info(f"[Agent0] 보고서 생성 요청 감지, Agent1 → Agent2 순서로 처리, user_input: {user_input}")
            
            # Bedrock Agent Runtime 호출
            client = boto3.client("bedrock-agent-runtime")
            session_attributes = {}
            
            # 1단계: Agent1 호출 (데이터 수집)
            logger.info(f"[Agent0] 1단계: Agent1({AGENT1_ID}) 호출 시작")
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
                agent1_result_text = "[Agent0] Agent1으로부터 유효한 응답을 받지 못했습니다."
            logger.info(f"[Agent0] Agent1 최종 응답 텍스트: {repr(agent1_result_text)[:500]}")

            # 2단계: Agent2 호출 (보고서 생성)
            target_agent_id = AGENT2_ID
            target_agent_alias = AGENT2_ALIAS
            logger.info(f"[Agent0] 2단계: Agent2({target_agent_id}) 호출 시작")

            # Agent1의 결과를 inputText에 명시적으로 포함
            agent2_input_text = f"보고서를 만들어주세요. 조회된 데이터:\n{agent1_result_text}"
            agent_kwargs = dict(
                agentId=target_agent_id,
                agentAliasId=target_agent_alias,
                sessionId=session_id,
                inputText=agent2_input_text,
                sessionState={
                    "sessionAttributes": session_attributes
                }
            )
            logger.info(f"[Agent0] Agent2 호출용 inputText: {agent2_input_text[:300]}")
            logger.info(f"[Agent0] Agent2 호출용 sessionAttributes: {json.dumps(session_attributes, ensure_ascii=False)[:300]}")

            try:
                response = client.invoke_agent(**agent_kwargs)
                # EventStream 객체 직접 파싱
                result = ""
                try:
                    for event in response:
                        if 'chunk' in event and 'bytes' in event['chunk']:
                            result += event['chunk']['bytes'].decode('utf-8')
                except Exception as e:
                    logger.error(f"EventStream 파싱 실패: {e}")
                    result = f"[Agent0] EventStream 파싱 실패: {str(e)}"
                if not result:
                    result = "[Agent0] Bedrock Agent 응답 파싱 실패 또는 빈 응답"
            except Exception as e:
                logger.error(f"Agent2 호출 실패: {e}")
                result = f"[Agent0] Agent2 호출 실패: {str(e)}"
            logger.info(f"[Agent0] Agent2 응답 완료, 결과 길이: {len(result) if result else 0}")
            return {
                'response': {
                    'body': {
                        'content': [
                            {
                                'type': 'text',
                                'text': result
                            }
                        ]
                    }
                }
            }

        else:
            # 단순 요금 조회: Agent1만 호출
            target_agent_id = AGENT1_ID
            target_agent_alias = AGENT1_ALIAS
            logger.info(f"[Agent0] 단순 요금 조회, Agent1({target_agent_id})만 호출, user_input: {user_input}")
            
            # Bedrock Agent Runtime 호출
            client = boto3.client("bedrock-agent-runtime")
            session_attributes = None
            conversation_history = []
        
        # Agent 호출 (Agent1 또는 Agent2)
        agent_kwargs = dict(
            agentId=target_agent_id,
            agentAliasId=target_agent_alias,
            sessionId=session_id,
            inputText=user_input
        )
        
        # 보고서 생성 요청인 경우에만 sessionState 추가 (Agent1 응답 포함)
        if is_report_request:
            session_state = {
                "sessionAttributes": session_attributes or {}
            }
            
            # conversationHistory가 있으면 추가
            if conversation_history:
                session_state["conversationHistory"] = conversation_history
                logger.info(f"[Agent0] conversationHistory 추가: {len(conversation_history)}개 메시지")
                logger.info(f"[Agent0] conversationHistory 내용: {json.dumps(conversation_history, ensure_ascii=False)[:500]}")
            
            agent_kwargs["sessionState"] = session_state
            logger.info(f"[Agent0] sessionState 추가 완료: {json.dumps(session_state, ensure_ascii=False)[:500]}")
        
        try:
            response = client.invoke_agent(**agent_kwargs)
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
        
        # AWS Bedrock Agent가 기대하는 응답 형식
        return {
            'response': {
                'body': {
                    'content': [
                        {
                            'type': 'text',
                            'text': result
                        }
                    ]
                }
            }
        }

    except Exception as e:
        logger.error(f"[Agent0] 에이전트 호출 중 오류: {e}", exc_info=True)
        return {
            'response': {
                'body': {
                    'content': [
                        {
                            'type': 'text',
                            'text': f'Agent0에서 에이전트 호출 중 오류: {str(e)}'
                        }
                    ]
                }
            }
        } 