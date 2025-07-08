
import json
import boto3
import logging
from typing import Dict, Any
from http import HTTPStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Agent0(슈퍼바이저) 람다: 무조건 Agent1(람다1)로만 위임하는 구조
    """
    try:
        # Agent1 람다 함수명 (AWS Lambda 콘솔의 실제 함수명으로 맞춰주세요)
        agent1_lambda_name = "fitcloudagent1_lambda"  # 실제 함수명으로 변경 필요

        logger.info(f"[Agent0] Agent1({agent1_lambda_name})로 위임 시작")
        client = boto3.client("lambda")
        response = client.invoke(
            FunctionName=agent1_lambda_name,
            Payload=json.dumps(event)
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
