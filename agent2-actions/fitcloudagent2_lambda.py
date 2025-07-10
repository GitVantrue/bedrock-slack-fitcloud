import json
import os
import boto3
import requests
import logging
from typing import Dict, Any
from http import HTTPStatus
import openpyxl
from openpyxl.chart import BarChart, Reference
import io
from collections import defaultdict

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Agent1 람다 이름
AGENT1_LAMBDA_NAME = os.environ.get("AGENT1_LAMBDA_NAME", "fitcloud_action_part1-wpfe6")

# 슬랙 토큰/채널ID를 환경변수에서 가져오기 (보안상 하드코딩 금지)
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')
SLACK_CHANNEL = os.environ.get('SLACK_CHANNEL')

# 환경변수 검증
if not SLACK_BOT_TOKEN:
    raise ValueError("SLACK_BOT_TOKEN 환경변수가 설정되지 않았습니다.")
if not SLACK_CHANNEL:
    raise ValueError("SLACK_CHANNEL 환경변수가 설정되지 않았습니다.")

def generate_excel_report(data):
    """
    데이터를 받아서 엑셀 보고서를 생성하고 슬랙에 업로드하는 함수
    """
    # app.py에서 개선된 데이터 검증 로직 적용
    if not data or not isinstance(data, list) or len(data) == 0:
        raise ValueError("유효하지 않은 데이터입니다. 리스트 형태의 데이터가 필요합니다.")

    records = data
    first = records[0]
    
    # 워크북 생성
    wb = openpyxl.Workbook()
    ws = wb.active
    
    # 데이터 구조 자동 판별 (app.py와 동일한 로직)
    excel_title = "AWS 리포트"
    ws_title = "리포트"
    headers = []
    rows = []
    chart = None
    chart_x_title = ''
    chart_y_title = ''
    chart_title = ''

    # 월별 요금
    if 'billingPeriod' in first:
        ws_title = "월별 요금 리포트"
        headers = ['월', '요금(USD)']
        months = [item['billingPeriod'] for item in records]
        costs = [float(item.get('usageFee', item.get('usageFeeUSD', 0))) for item in records]
        rows = list(zip(months, costs))
        chart_x_title = '월'
        chart_y_title = '요금(USD)'
        chart_title = '월별 요금'
    # 일별 요금
    elif 'date' in first or 'dailyDate' in first:
        ws_title = "일별 요금 리포트"
        headers = ['일', '요금(USD)']
        days = [item.get('date', item.get('dailyDate')) for item in records]
        costs = [float(item.get('usageFee', item.get('usageFeeUSD', 0))) for item in records]
        rows = list(zip(days, costs))
        chart_x_title = '일'
        chart_y_title = '요금(USD)'
        chart_title = '일별 요금'
    # 계정별 요금
    elif 'accountId' in first:
        ws_title = "계정별 요금 리포트"
        headers = ['계정ID', '요금(USD)']
        accounts = [item['accountId'] for item in records]
        costs = [float(item.get('usageFee', item.get('usageFeeUSD', 0))) for item in records]
        rows = list(zip(accounts, costs))
        chart_x_title = '계정ID'
        chart_y_title = '요금(USD)'
        chart_title = '계정별 요금'
    # 태그별 요금 등 기타 케이스(확장 가능) - app.py와 동일한 주석
    elif 'tagsJson' in first:
        ws_title = "태그별 요금 리포트"
        headers = ['태그', '요금(USD)']
        tags = []
        costs = []
        for item in records:
            tag_str = ', '.join([f'{k}:{v}' for k, v in item['tagsJson'].items()]) if isinstance(item['tagsJson'], dict) else str(item['tagsJson'])
            tags.append(tag_str)
            costs.append(float(item.get('usageFee', item.get('usageFeeUSD', 0))))
        rows = list(zip(tags, costs))
        chart_x_title = '태그'
        chart_y_title = '요금(USD)'
        chart_title = '태그별 요금'
    else:
        # 모든 필드를 헤더로, 각 row를 값으로
        headers = list(first.keys())
        rows = [[item.get(h, '') for h in headers] for item in records]
        ws_title = "일반 리포트"
        chart = None  # 차트 미생성

    ws.title = ws_title
    ws.append(headers)
    for row in rows:
        ws.append(row)

    # 차트 추가 (가능한 경우만) - app.py와 동일한 로직
    if not chart and len(rows) > 0 and len(headers) == 2 and all(isinstance(r[1], (int, float)) for r in rows):
        chart = BarChart()
        chart.title = chart_title
        chart.x_axis.title = chart_x_title
        chart.y_axis.title = chart_y_title
        data_ref = Reference(ws, min_col=2, min_row=1, max_row=len(rows)+1)
        cats_ref = Reference(ws, min_col=1, min_row=2, max_row=len(rows)+1)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        ws.add_chart(chart, "E2")

    # 파일 메모리 저장
    file_stream = io.BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)

    file_name = 'report.xlsx'
    mime_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    file_stream_value = file_stream.getvalue()
    file_size = len(file_stream_value)

    # 슬랙 파일 업로드 (app.py와 동일한 개선된 로직)
    try:
        headers_get_url = {
            'Authorization': f'Bearer {SLACK_BOT_TOKEN}'
        }
        files_data = {
            'filename': (None, file_name),
            'length': (None, str(file_size)),
            'filetype': (None, 'xlsx')
        }
        
        # 1. 업로드 URL 가져오기
        get_upload_url_response = requests.post(
            'https://slack.com/api/files.getUploadURLExternal',
            headers=headers_get_url,
            files=files_data
        )
        get_upload_url_result = get_upload_url_response.json()
        if not get_upload_url_result.get('ok'):
            error_msg = get_upload_url_result.get('error')
            raise Exception(f'파일 업로드 URL을 가져오는 데 실패했습니다: {error_msg}')
            
        upload_url = get_upload_url_result['upload_url']
        file_id = get_upload_url_result['file_id']
        
        # 2. 파일 콘텐츠 업로드
        file_stream.seek(0)
        files = {
            'file': (file_name, file_stream, mime_type)
        }
        upload_file_response = requests.post(
            upload_url,
            files=files
        )
        if not upload_file_response.ok:
            raise Exception(f'파일 콘텐츠 업로드에 실패했습니다: {upload_file_response.text}')
            
        # 3. 업로드 완료
        headers_complete_upload = {
            'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
            'Content-Type': 'application/json'
        }
        payload_complete_upload = {
            'files': [{'id': file_id, 'title': file_name}],
            'channel_id': SLACK_CHANNEL,
            'initial_comment': f'📊 {ws_title}가 생성되었습니다.'
        }
        complete_upload_response = requests.post(
            'https://slack.com/api/files.completeUploadExternal',
            headers=headers_complete_upload,
            json=payload_complete_upload
        )
        complete_upload_result = complete_upload_response.json()
        if not complete_upload_result.get('ok'):
            error_msg = complete_upload_result.get('error')
            if error_msg == 'not_in_channel':
                raise Exception('봇이 채널에 추가되지 않았습니다. 슬랙 채널에 봇을 추가해주세요.')
            elif error_msg == 'channel_not_found':
                raise Exception('채널을 찾을 수 없습니다. 채널 ID를 확인해주세요.')
            else:
                raise Exception(f'파일 업로드를 완료하는 데 실패했습니다: {error_msg}')
                
        permalink = None
        if complete_upload_result.get('files') and len(complete_upload_result['files']) > 0:
            permalink = complete_upload_result['files'][0].get('permalink')
            
        return {
            'success': True,
            'message': '파일 업로드 및 채널 공유 성공',
            'file_id': file_id,
            'permalink': permalink,
            'report_title': ws_title
        }
        
    except requests.exceptions.RequestException as e:
        raise Exception(f'네트워크 요청 오류: {e}')
    except Exception as e:
        raise Exception(f'예상치 못한 오류 발생: {e}')

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Agent2(리포트/엑셀/슬랙 업로드) 람다
    1. 입력 파라미터(기간, 계정, 태그 등) 파싱
    2. Agent1 람다 호출하여 표 데이터 조회
    3. 엑셀 보고서 생성 및 슬랙 업로드
    4. 결과 반환
    """
    try:
        logger.info(f"[Agent2] event 구조: {json.dumps(event, ensure_ascii=False)}")
        
        # 1. 파라미터 추출 (event 구조에 따라 보강)
        params = None
        # 1-1. parameters가 dict로 들어오는 경우
        if isinstance(event.get("parameters"), dict):
            params = event["parameters"]
        # 1-2. parameters가 list이거나 없을 때, requestBody에서 추출
        if not params:
            try:
                props = event["requestBody"]["content"]["application/json"]["properties"]
                for prop in props:
                    if prop.get("name") == "user_input":
                        params = {"user_input": prop.get("value")}
                        break
            except Exception as e:
                logger.error(f"user_input 추출 실패: {e}")
        # 1-3. 그래도 없으면 inputText 등 다른 필드 시도
        if not params:
            params = event.get("user_input") or event.get("inputText") or event
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {"user_input": params}
        logger.info(f"[Agent2] 입력 파라미터: {params}")

        # === sessionAttributes에서 표/메시지 활용 시도 ===
        session_attrs = event.get("sessionAttributes", {})
        last_cost_table = session_attrs.get("last_cost_table")
        last_cost_message = session_attrs.get("last_cost_message")
        used_session = False
        report_data = None
        
        if last_cost_table:
            try:
                report_data = json.loads(last_cost_table)
                used_session = True
                logger.info(f"[Agent2] sessionAttributes에서 표/메시지 활용")
            except Exception as e:
                logger.error(f"[Agent2] last_cost_table 파싱 실패: {e}")
                
        # === sessionAttributes 값이 없으면 기존처럼 Agent1 람다 호출 ===
        if not report_data:
            client = boto3.client("lambda")
            logger.info("[Agent2] Agent1 람다 호출 시작")
            try:
                agent1_response = client.invoke(
                    FunctionName=AGENT1_LAMBDA_NAME,
                    InvocationType='RequestResponse',
                    Payload=json.dumps(event)
                )
                logger.info("[Agent2] Agent1 람다 호출 완료")
            except Exception as e:
                logger.error(f"[Agent2] Agent1 람다 호출 실패: {e}")
                raise
            try:
                agent1_result = json.load(agent1_response['Payload'])
                logger.info(f"[Agent2] Agent1 람다 응답: {agent1_result}")
                # Bedrock Agent 람다 응답 구조에 따라 body 파싱 (app.py 스타일로 개선)
                if 'response' in agent1_result and 'responseBody' in agent1_result['response']['application/json']:
                    body_str = agent1_result['response']['application/json']['body']
                    try:
                        body_json = json.loads(body_str)
                        # app.py와 동일한 데이터 추출 로직 적용
                        report_data = body_json.get('cost_items') or body_json.get('data') or body_json
                    except Exception as e:
                        logger.error(f"[Agent2] body_str 파싱 실패: {e}")
                        report_data = body_str
                else:
                    report_data = agent1_result.get('body') or agent1_result
            except Exception as e:
                logger.error(f"[Agent2] Agent1 람다 응답 파싱 실패: {e}")
                raise

        # app.py 스타일의 데이터 검증 추가
        if not report_data or not isinstance(report_data, list) or len(report_data) == 0:
            raise ValueError("유효하지 않은 데이터입니다. 리스트 형태의 데이터가 필요합니다.")

        # 3. 엑셀 보고서 생성 및 슬랙 업로드
        logger.info(f"[Agent2] 엑셀 보고서 생성 시작")
        try:
            upload_result = generate_excel_report(report_data)
            logger.info(f"[Agent2] 엑셀 보고서 생성 완료: {upload_result}")
        except Exception as e:
            logger.error(f"[Agent2] 엑셀 보고서 생성 실패: {e}")
            raise

        # 4. 결과 반환
        debug_info = {
            'used_session': used_session,
            'session_last_cost_table': last_cost_table,
            'session_last_cost_message': last_cost_message,
            'upload_result': upload_result
        }

        # Bedrock Agent 응답 형식으로 반환
        completion_msg = (
            f"📊 **{upload_result.get('report_title', '리포트')} 생성 완료!**\n"
            f"✅ 엑셀 파일이 슬랙 채널에 업로드되었습니다.\n"
            f"🔗 파일 링크: {upload_result.get('permalink', '링크 없음')}\n"
            f"📁 파일 ID: {upload_result.get('file_id', 'N/A')}\n"
            f"📋 데이터 소스: {'세션 속성' if used_session else 'Agent1 호출'}"
        )
        
        return {
            'completion': completion_msg
        }

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"[Agent2] 처리 중 오류: {e}\n{tb}", exc_info=True)
        return {
            'completion': f'❌ [Agent2] 처리 중 오류: {str(e)}'
        } 