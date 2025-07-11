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
    
    # --- 키워드 체크 및 분기 로직 추가 ---
    report_keywords = ["보고서", "리포트", "엑셀", "차트", "그래프", "PDF", "파일", "첨부", "다운로드", "업로드", "슬랙", "만들어", "생성", "제작"]
    user_input_lower = user_input.lower()
    is_report_request = any(keyword in user_input_lower for keyword in report_keywords)
    
    logger.info(f"[Supervisor] 사용자 입력: '{user_input}'")
    logger.info(f"[Supervisor] 보고서 요청 여부: {is_report_request}")
    
    # Lambda 클라이언트 (Agent1 직접 호출용)
    lambda_client = boto3.client("lambda")
    
    if is_report_request:
        # 보고서 요청: Agent1 → Agent2 순서로 처리
        logger.info(f"[Supervisor] 보고서 요청 감지. Agent1 → Agent2 순서로 처리 시작")
        
        try:
            # 1. Agent1 Lambda 직접 호출
            logger.info(f"[Supervisor] Agent1 Lambda 직접 호출 시작")
            agent1_lambda_name = "fitcloud_action_part1-wpfe6"  # Agent1 람다 함수명
            
            agent1_payload = {
                "inputText": user_input,
                "sessionId": session_id,
                "sessionAttributes": event.get("sessionAttributes", {}),
                "parameters": event.get("parameters", {}),
                "requestBody": event.get("requestBody", {}),
                "httpMethod": "POST",
                "apiPath": "/costs/ondemand/corp/monthly"
            }
            
            agent1_response = lambda_client.invoke(
                FunctionName=agent1_lambda_name,
                InvocationType='RequestResponse',
                Payload=json.dumps(agent1_payload)
            )
            
            # 1. Agent1 Lambda 응답 처리
            agent1_response_payload = json.loads(agent1_response['Payload'].read().decode('utf-8'))
            logger.info(f"[Supervisor] Agent1 Lambda 응답 상태: {agent1_response['StatusCode']}")
            
            # Agent1 응답에서 실제 데이터 추출
            raw_agent1_response = ""
            if 'response' in agent1_response_payload:
                response_body = agent1_response_payload['response']
                if 'body' in response_body and 'content' in response_body['body']:
                    content = response_body['body']['content']
                    if isinstance(content, list) and len(content) > 0:
                        raw_agent1_response = content[0].get('text', '')
            
            # Agent1 원본 응답 로그 추가
            logger.info(f"[Supervisor] Agent1 원본 응답 (처음 500자): {raw_agent1_response[:500]}")
            logger.info(f"[Supervisor] Agent1 원본 응답 길이: {len(raw_agent1_response)}")
            
            # Agent1 응답 검증
            if not raw_agent1_response or len(raw_agent1_response.strip()) == 0:
                logger.error("[Supervisor] Agent1 응답이 비어있습니다.")
                return {
                    'response': {
                        'body': {
                            'content': [
                                {
                                    'type': 'text',
                                    'text': '❌ Agent1에서 데이터를 조회할 수 없습니다. 잠시 후 다시 시도해주세요.'
                                }
                            ]
                        }
                    }
                }
            
            # 2. 마크다운 텍스트만 추출 (JSON 파싱 우선)
            agent1_result_text = extract_markdown_from_agent1(raw_agent1_response)
            logger.info(f"[Supervisor] Agent1 추출된 텍스트 (처음 300자): {agent1_result_text[:300]}")
            logger.info(f"[Supervisor] Agent1 추출된 텍스트 길이: {len(agent1_result_text)}")
            
            # 3. Agent2 호출 (동일한 sessionId + sessionAttributes에 Agent1 응답 저장)
            agent2_input_text = f"보고서를 만들어주세요. 조회된 데이터:\n{agent1_result_text}"
            logger.info(f"[Supervisor] Agent2 호출용 inputText: {agent2_input_text[:300]}")
            logger.info(f"[Supervisor] Agent2 호출 시 sessionId: {session_id}")
            
            try:
                # Agent2 Lambda 직접 호출
                agent2_lambda_name = "fitcloud_action_part2-wpfe6"  # Agent2 람다 함수명
                
                agent2_payload = {
                    "inputText": agent2_input_text,
                    "sessionId": session_id,
                    "sessionAttributes": {
                        "agent1_response": agent1_result_text,
                        "agent1_raw_response": raw_agent1_response,
                        "supervisor_session": "true",
                        "report_request": "true"
                    },
                    "parameters": event.get("parameters", {}),
                    "requestBody": event.get("requestBody", {})
                }
                
                agent2_response = lambda_client.invoke(
                    FunctionName=agent2_lambda_name,
                    InvocationType='RequestResponse',
                    Payload=json.dumps(agent2_payload)
                )
                logger.info(f"[Supervisor] Agent2 Lambda 호출 성공")
                
                # Agent2 응답 처리
                agent2_response_payload = json.loads(agent2_response['Payload'].read().decode('utf-8'))
                logger.info(f"[Supervisor] Agent2 Lambda 응답 상태: {agent2_response['StatusCode']}")
                
                agent2_result = ""
                if 'response' in agent2_response_payload:
                    response_body = agent2_response_payload['response']
                    if 'body' in response_body and 'content' in response_body['body']:
                        content = response_body['body']['content']
                        if isinstance(content, list) and len(content) > 0:
                            agent2_result = content[0].get('text', '')
                
                if not agent2_result:
                    agent2_result = "[Supervisor] Agent2로부터 유효한 응답을 받지 못했습니다."
                logger.info(f"[Supervisor] Agent2 최종 응답: {agent2_result[:300]}")
                
            except Exception as agent2_e:
                logger.error(f"[Supervisor] Agent2 호출 실패: {agent2_e}")
                return {
                    'response': {
                        'body': {
                            'content': [
                                {
                                    'type': 'text',
                                    'text': f'❌ Agent2 호출 중 오류가 발생했습니다: {str(agent2_e)}'
                                }
                            ]
                        }
                    }
                }
            
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
            
        except Exception as e:
            logger.error(f"[Supervisor] 보고서 생성 중 오류: {e}")
            import traceback
            logger.error(f"[Supervisor] 보고서 생성 오류 상세: {traceback.format_exc()}")
            
            # 에러 발생 시 사용자 친화적 메시지 반환
            error_message = f"보고서 생성 중 오류가 발생했습니다: {str(e)}"
            return {
                'response': {
                    'body': {
                        'content': [
                            {
                                'type': 'text',
                                'text': error_message
                            }
                        ]
                    }
                }
            }
    else:
        # 단순 조회 요청: Agent1만 호출
        logger.info(f"[Supervisor] 단순 조회 요청 감지. Agent1만 호출")
        
        try:
            # Agent1 Lambda 직접 호출
            logger.info(f"[Supervisor] Agent1 Lambda 직접 호출 시작")
            agent1_lambda_name = "fitcloud_action_part1-wpfe6"  # Agent1 람다 함수명
            
            agent1_payload = {
                "inputText": user_input,
                "sessionId": session_id,
                "sessionAttributes": event.get("sessionAttributes", {}),
                "parameters": event.get("parameters", {}),
                "requestBody": event.get("requestBody", {}),
                "httpMethod": "POST",
                "apiPath": "/costs/ondemand/corp/monthly"
            }
            
            agent1_response = lambda_client.invoke(
                FunctionName=agent1_lambda_name,
                InvocationType='RequestResponse',
                Payload=json.dumps(agent1_payload)
            )
            
            # Agent1 Lambda 응답 처리
            agent1_response_payload = json.loads(agent1_response['Payload'].read().decode('utf-8'))
            logger.info(f"[Supervisor] Agent1 Lambda 응답 상태: {agent1_response['StatusCode']}")
            
            # Agent1 응답에서 실제 데이터 추출
            raw_agent1_response = ""
            if 'response' in agent1_response_payload:
                response_body = agent1_response_payload['response']
                if 'body' in response_body and 'content' in response_body['body']:
                    content = response_body['body']['content']
                    if isinstance(content, list) and len(content) > 0:
                        raw_agent1_response = content[0].get('text', '')
            
            logger.info(f"[Supervisor] Agent1 원본 응답 (처음 500자): {raw_agent1_response[:500]}")
            logger.info(f"[Supervisor] Agent1 원본 응답 길이: {len(raw_agent1_response)}")
            
            # Agent1 응답을 적절히 파싱하여 반환 (기존 형식 유지)
            agent1_result_text = extract_markdown_from_agent1(raw_agent1_response)
            logger.info(f"[Supervisor] Agent1 파싱된 응답 (처음 300자): {agent1_result_text[:300]}")
            
            # 최종 응답 반환
            return {
                'response': {
                    'body': {
                        'content': [
                            {
                                'type': 'text',
                                'text': agent1_result_text
                            }
                        ]
                    }
                }
            }
            
        except Exception as e:
            logger.error(f"[Supervisor] Agent1 호출 중 오류: {e}")
            import traceback
            logger.error(f"[Supervisor] Agent1 호출 오류 상세: {traceback.format_exc()}")
            
            # 에러 발생 시 사용자 친화적 메시지 반환
            error_message = f"비용 조회 중 오류가 발생했습니다: {str(e)}"
            return {
                'response': {
                    'body': {
                        'content': [
                            {
                                'type': 'text',
                                'text': error_message
                            }
                        ]
                    }
                }
            }

def extract_markdown_from_agent1(raw_response: str) -> str:
    """
    Agent1의 응답에서 사용자에게 보여줄 텍스트를 추출합니다.
    다양한 응답 형식을 처리하여 안정적으로 동작합니다.
    """
    if not raw_response or not raw_response.strip():
        logger.warning("[Supervisor] Agent1 응답이 비어있습니다.")
        return "Agent1에서 응답을 받지 못했습니다."
    
    try:
        # 1. JSON 파싱 시도 (Bedrock Agent 응답 형식)
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
        
        # 2. [RESPONSE][message] 패턴 시도
        match = re.search(r"\[RESPONSE\]\[message\](.*)", raw_response, re.DOTALL)
        if match:
            logger.info("[Supervisor] [RESPONSE][message] 패턴으로 텍스트 추출 성공.")
            return match.group(1).strip()
        
        # 3. 마크다운 패턴 시도 (개선된 버전)
        # *━━━━━━━━━━━━━━━━━━━━━━* 로 시작하는 패턴
        md_match = re.search(r"(\*━━━━━━━━+.*?)(?:END RequestId|$)", raw_response, re.DOTALL)
        if md_match:
            logger.info("[Supervisor] 마크다운 패턴으로 텍스트 추출 성공.")
            return md_match.group(1).strip()
        
        # 4. 새로운 패턴: *📅 AWS 법인 전체 요금* 로 시작하는 패턴
        aws_cost_match = re.search(r"(\*📅 AWS.*?)(?:END RequestId|$)", raw_response, re.DOTALL)
        if aws_cost_match:
            logger.info("[Supervisor] AWS 비용 패턴으로 텍스트 추출 성공.")
            return aws_cost_match.group(1).strip()
        
        # 5. 일반적인 마크다운 응답 패턴 (더 포괄적)
        general_md_match = re.search(r"(\*.*?)(?:END RequestId|$)", raw_response, re.DOTALL)
        if general_md_match:
            logger.info("[Supervisor] 일반 마크다운 패턴으로 텍스트 추출 성공.")
            return general_md_match.group(1).strip()
        
        # 6. 마지막 시도: 전체 응답에서 의미있는 텍스트 부분만 추출
        # 줄바꿈으로 구분된 텍스트 중에서 실제 내용이 있는 부분만
        lines = raw_response.split('\n')
        meaningful_lines = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith('END RequestId') and not line.startswith('REPORT'):
                meaningful_lines.append(line)
        
        if meaningful_lines:
            logger.info("[Supervisor] 의미있는 라인들로 텍스트 추출 성공.")
            return '\n'.join(meaningful_lines)
        
        logger.warning("[Supervisor] 모든 패턴으로 텍스트 추출 실패. Agent1 원본 응답 반환.")
        # 원본 응답이 너무 길면 잘라서 반환
        if len(raw_response) > 2000:
            return raw_response[:2000] + "\n\n... (응답이 너무 길어 일부만 표시됩니다)"
        return raw_response.strip()
    except Exception as e:
        logger.error(f"[Supervisor] Agent1 응답 파싱 중 예상치 못한 오류: {e}")
        # 파싱 실패 시 원본 응답 반환 (안전장치)
        if len(raw_response) > 2000:
            return raw_response[:2000] + "\n\n... (응답이 너무 길어 일부만 표시됩니다)"
        return raw_response.strip() 