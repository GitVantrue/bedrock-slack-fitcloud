import json
import boto3
import logging
from typing import Dict, Any
from http import HTTPStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Agent0(슈퍼바이저) 람다: parameters.user_input을 받아 Agent1(람다1)로 위임
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

        agent1_lambda_name = "fitcloud_action_part1-wpfe6"  # 실제 함수명으로 변경 필요

        logger.info(f"[Agent0] Agent1({agent1_lambda_name})로 위임 시작, user_input: {user_input}")
        # Agent1로 전달할 event 구성 (user_input만 전달)
        agent1_event = {
            "parameters": {
                "user_input": user_input
            }
        }
        client = boto3.client("lambda")
        response = client.invoke(
            FunctionName=agent1_lambda_name,
            Payload=json.dumps(agent1_event)
        )
        result = json.load(response['Payload'])
        logger.info(f"[Agent0] Agent1 응답: {result}")
        return result

    except Exception as e:
        logger.error(f"[Agent0] Agent1 위임 중 오류: {e}", exc_info=True)
        return {
            'statusCode': HTTPStatus.INTERNAL_SERVER_ERROR,
            'body': f'Agent0에서 Agent1 호출 중 오류: {str(e)}'
        } 