import json
import os
import boto3
import logging
from typing import Dict, Any
from http import HTTPStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 람다 이름 환경변수 또는 하드코딩
AGENT1_LAMBDA_NAME = os.environ.get("AGENT1_LAMBDA_NAME", "fitcloud_action_part1-wpfe6")
AGENT2_LAMBDA_NAME = os.environ.get("AGENT2_LAMBDA_NAME", "fitcloudagent2_lambda")
AGENT2_KEYWORDS = ["보고서", "리포트", "엑셀", "차트", "그래프", "PDF", "파일", "첨부", "다운로드", "업로드", "슬랙"]

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Agent0(슈퍼바이저) 람다: parameters.user_input을 받아 Agent1 또는 Agent2로 분기 호출
    """
    try:
        # user_input 파라미터 필수 체크
        user_input = event.get("parameters", {}).get("user_input")
        if not user_input:
            logger.error("[Agent0] user_input 파라미터가 없습니다.")
            return {
                'statusCode': HTTPStatus.BAD_REQUEST,
                'body': 'user_input 파라미터가 필요합니다.'
            }

        # 분기: Agent2 키워드 포함 여부
        if any(keyword in user_input for keyword in AGENT2_KEYWORDS):
            target_lambda = AGENT2_LAMBDA_NAME
            logger.info(f"[Agent0] Agent2({target_lambda})로 위임 시작, user_input: {user_input}")
        else:
            target_lambda = AGENT1_LAMBDA_NAME
            logger.info(f"[Agent0] Agent1({target_lambda})로 위임 시작, user_input: {user_input}")

        # Agent1/2로 전달할 event 구성
        lambda_event = {
            "parameters": {
                "user_input": user_input
            }
        }
        client = boto3.client("lambda")
        response = client.invoke(
            FunctionName=target_lambda,
            Payload=json.dumps(lambda_event)
        )
        result = json.load(response['Payload'])
        logger.info(f"[Agent0] {target_lambda} 응답: {result}")
        # statusCode/body 구조로 반환
        if isinstance(result, dict) and "statusCode" in result and "body" in result:
            return result
        return {
            'statusCode': 200,
            'body': json.dumps(result, ensure_ascii=False)
        }

    except Exception as e:
        logger.error(f"[Agent0] 람다 위임 중 오류: {e}", exc_info=True)
        return {
            'statusCode': HTTPStatus.INTERNAL_SERVER_ERROR,
            'body': f'Agent0에서 람다 호출 중 오류: {str(e)}'
        } 