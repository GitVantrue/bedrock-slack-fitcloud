import json
import os
import requests
import boto3
from urllib.parse import parse_qs
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from datetime import datetime, timedelta
import pytz # KST 시간대 처리를 위해 pytz 라이브러리 추가

# 환경 변수에서 FitCloud API 기본 URL 및 Secrets Manager 보안 암호 가져오기
FITCLOUD_BASE_URL = os.environ.get('FITCLOUD_BASE_URL', 'https://aws-dev.fitcloud.co.kr/api/v1')
SECRET_NAME = os.environ.get('FITCLOUD_API_SECRET_NAME', 'dev-FitCloud/ApiToken')

# Secrets Manager 클라이언트 초기화
secrets_client = boto3.client('secretsmanager')

# 토큰 캐싱을 위한 전역 변수
FITCLOUD_API_TOKEN = None

# --- 최적화 관련 상수 설정 ---
# MAX_RESPONSE_SIZE_BYTES = 10000 # 현재 코드에서 직접 사용되지 않음
SUMMARY_ITEM_COUNT_THRESHOLD = 20  # 더 많은 항목을 허용

def get_current_date_info():
    """현재 날짜 정보를 KST(한국 표준시) 기준으로 반환합니다."""
    # 디버깅을 위한 상세 로깅 추가
    import os
    
    print(f"🔍 Lambda 1 시간대 디버깅 정보:")
    print(f"  - 시스템 TZ 환경변수: {os.environ.get('TZ', '설정되지 않음')}")
    print(f"  - UTC 시간: {datetime.utcnow()}")
    print(f"  - 로컬 시간 (시스템): {datetime.now()}")
    
    # 여러 방법으로 KST 시간 계산 (일관성 확인)
    utc_now = datetime.utcnow()
    tz = pytz.timezone('Asia/Seoul')
    
    # 방법 1: UTC 기반 변환
    utc_with_tz = pytz.utc.localize(utc_now)
    now_method1 = utc_with_tz.astimezone(tz)
    
    # 방법 2: 직접 KST 계산
    now_method2 = datetime.now(tz)
    
    # 방법 3: 수동 KST 계산 (UTC + 9시간)
    kst_offset = timedelta(hours=9)
    now_method3 = utc_now + kst_offset
    
    print(f"  - 방법 1 (UTC→KST 변환): {now_method1}")
    print(f"  - 방법 2 (직접 KST): {now_method2}")
    print(f"  - 방법 3 (수동 +9시간): {now_method3}")
    
    # 가장 안정적인 방법 선택 (방법 1)
    now = now_method1
    
    # 일관성 검증
    if now_method1.date() != now_method2.date():
        print(f"⚠️ 경고: Lambda 1 시간대 계산 방법 간 차이 발견!")
        print(f"  - 방법 1: {now_method1.date()}")
        print(f"  - 방법 2: {now_method2.date()}")
    
    print(f"🕐 Lambda 1 최종 현재 시간 정보:")
    print(f"  - 현재 날짜/시간: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  - 현재 날짜: {now.year}년 {now.month}월 {now.day}일")
    
    return {
        'current_year': now.year,
        'current_month': now.month,
        'current_day': now.day,
        'current_datetime': now, # 시간대 정보 포함된 datetime 객체
        'current_date_str': now.strftime('%Y%m%d'),  # YYYYMMDD 형식
        'current_month_str': now.strftime('%Y%m'),   # YYYYMM 형식
        'utc_time': utc_now.isoformat(),  # UTC 시간도 포함
        'kst_time': now.isoformat()       # KST 시간도 포함
    }

def smart_date_correction(params):
    """
    사용자 의도에 맞게 날짜 파라미터를 보정합니다.
    Agent가 잘못 추론한 연도를 올바르게 수정하며, 연도가 없는 경우 현재 연도를 추가 시도합니다.
    """
    current_info = get_current_date_info()
    current_year = current_info['current_year']
    current_month_str = f"{current_info['current_month']:02d}"
    current_day_str = f"{current_info['current_day']:02d}"

    print(f"🗓️ 현재 날짜 정보 (smart_date_correction 내부): {current_year}년 {current_month_str}월 {current_day_str}일")
    
    corrected_params = params.copy()
    
    # 'from' 또는 'to' 파라미터가 없는 경우, 현재 날짜를 기본값으로 설정
    if 'from' not in corrected_params and 'to' not in corrected_params:
        today_str = f"{current_year}{current_month_str}{current_day_str}"
        corrected_params['from'] = today_str
        corrected_params['to'] = today_str
        print(f"➕ 날짜 파라미터 없음. 오늘 날짜로 기본값 설정: from={today_str}, to={today_str}")

    for param_name in ['from', 'to']:
        original_value = str(corrected_params.get(param_name, '')) # params.get()으로 안전하게 접근
        
        # 값이 비어있으면 건너뜀 (위에서 기본값 설정 후에도 여전히 비어있다면)
        if not original_value.strip():
            print(f"➡️ {param_name} 값이 비어있어 보정을 건너뜀.")
            continue

        # 월만 입력된 경우(예: '5', '05', '6', '06')
        if len(original_value) == 1 or (len(original_value) == 2 and original_value.isdigit()):
            # 1~12월로 인식
            month_str = original_value.zfill(2)
            yyyymm = f"{current_year}{month_str}"
            corrected_params[param_name] = yyyymm
            print(f"🔄 {param_name} 보정됨 (월만 입력 → YYYYMM): {original_value} → {yyyymm}")
            continue

        # MMDD 형태 (예: '0603')
        if len(original_value) == 4 and original_value.isdigit():
            test_date_str = str(current_year) + original_value
            try:
                datetime.strptime(test_date_str, '%Y%m%d') # 유효한 날짜인지 확인
                corrected_params[param_name] = test_date_str
                print(f"🔄 {param_name} 보정됨 (MMDD -> YYYYMMDD): {original_value} → {test_date_str}")
                continue
            except ValueError:
                print(f"❌ {param_name} '{original_value}'는 유효한 MMDD 형식이 아니거나 연도 추가 후 유효하지 않음.")
                pass

        # YYYYMMDD 또는 YYYYMM 형식에서 연도 보정
        if len(original_value) == 8 or len(original_value) == 6:
            year_part = original_value[:4]
            suffix_part = original_value[4:]
            try:
                # 입력된 연도가 현재 연도보다 이전이고, 너무 과거가 아니라면 현재 연도로 보정 시도
                if int(year_part) < current_year and int(year_part) >= 2020:
                    corrected_value = str(current_year) + suffix_part
                    # 보정된 날짜가 유효한지 최종 확인
                    if len(corrected_value) == 8:
                        datetime.strptime(corrected_value, '%Y%m%d')
                    elif len(corrected_value) == 6:
                        datetime.strptime(corrected_value + '01', '%Y%m%d')
                    corrected_params[param_name] = corrected_value
                    print(f"🔄 {param_name} 보정됨 (이전 연도 -> 현재 연도): {original_value} → {corrected_value}")
                else:
                    print(f"➡️ {param_name} 연도 {year_part}는 보정 대상이 아니거나 이미 올바름.")
            except ValueError:
                print(f"⚠️ {param_name} '{original_value}' 연도 부분 '{year_part}'이 숫자가 아니거나 보정 후 날짜가 유효하지 않습니다.")
                pass
        else:
            print(f"⚠️ {param_name} '{original_value}'는 예상된 날짜 형식이 아닙니다. 보정을 건너뜀.")

    return corrected_params

def validate_date_logic(params):
    """
    보정된 날짜의 논리적 타당성을 검증합니다.
    미래 날짜나 잘못된 날짜 범위를 확인합니다.
    """
    current_info = get_current_date_info()
    current_date_only = current_info['current_datetime'].date() 

    warnings = []
    
    # 'from'과 'to' 파라미터가 모두 존재할 때만 날짜 유효성 검증
    if 'from' in params and 'to' in params:
        from_str = str(params['from'])
        to_str = str(params['to'])
        
        try:
            is_daily_format = False
            from_dt_obj = None
            to_dt_obj = None

            if len(from_str) == 8 and len(to_str) == 8:  # YYYYMMDD 형식
                from_dt_obj = datetime.strptime(from_str, '%Y%m%d').date()
                to_dt_obj = datetime.strptime(to_str, '%Y%m%d').date()
                is_daily_format = True
            elif len(from_str) == 6 and len(to_str) == 6:  # YYYYMM 형식
                from_dt_obj = datetime.strptime(from_str + '01', '%Y%m%d').date()
                # to_dt_obj는 해당 월의 마지막 날짜로 설정하여 비교
                next_month = (datetime.strptime(to_str + '01', '%Y%m%d').replace(day=1) + timedelta(days=32)).replace(day=1)
                to_dt_obj = (next_month - timedelta(days=1)).date()
            else:
                warnings.append("날짜 형식이 올바르지 않습니다 (YYYYMM 또는 YYYYMMDD).")
                return warnings
            
            # 조회 기간 시작일이 종료일보다 늦을 경우
            if from_dt_obj > to_dt_obj:
                warnings.append("조회 시작일이 종료일보다 늦습니다.")

            # 미래 날짜/월 체크 (현재 날짜를 기준으로 판단)
            if is_daily_format:
                # 시작 날짜 또는 종료 날짜가 오늘보다 미래인 경우
                if from_dt_obj > current_date_only or to_dt_obj > current_date_only:
                    warnings.append(f"요청하신 날짜가 미래입니다: {from_str} - {to_str}")
            else: # 월별
                # 요청된 월의 연도와 월을 추출
                req_from_year = int(from_str[:4])
                req_from_month = int(from_str[4:])
                req_to_year = int(to_str[:4])
                req_to_month = int(to_str[4:])
                # 현재 연도와 월을 기준으로 미래인지 판단 (오늘이 속한 월보다 이후 월만 미래로 간주)
                current_year = current_info['current_year']
                current_month = current_info['current_month']
                is_from_future_month = (req_from_year > current_year) or (req_from_year == current_year and req_from_month > current_month)
                is_to_future_month = (req_to_year > current_year) or (req_to_year == current_year and req_to_month > current_month)
                # 오늘이 속한 월(YYYYMM)까지는 미래로 간주하지 않음
                if is_from_future_month or is_to_future_month:
                    warnings.append(f"요청하신 월이 미래입니다: {from_str} - {to_str}")
                    
        except ValueError as e:
            warnings.append(f"날짜 파싱 오류: {e}. 유효한 날짜 형식을 입력해주세요.")
    else:
        # from 또는 to 중 하나라도 없으면 경고 (smart_date_correction에서 기본값을 채웠어야 함)
        if 'from' not in params or 'to' not in params:
            warnings.append("조회를 위한 '시작 날짜' 및 '종료 날짜'가 모두 필요합니다.")

    if warnings:
        print(f"⚠️ 날짜 검증 경고: {warnings}")
    
    return warnings

def create_retry_session(retries=3, backoff_factor=0.3, status_forcelist=(500, 502, 504)):
    """재시도 로직이 포함된 requests 세션을 생성합니다."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def get_fitcloud_token():
    """Secrets Manager에서 FitCloud API 토큰을 가져옵니다."""
    global FITCLOUD_API_TOKEN
    if FITCLOUD_API_TOKEN is None:
        try:
            get_secret_value_response = secrets_client.get_secret_value(SecretId=SECRET_NAME)
            if 'SecretString' in get_secret_value_response:
                secret = json.loads(get_secret_value_response['SecretString'])
                FITCLOUD_API_TOKEN = secret.get('fitcloud_api_token')
                if not FITCLOUD_API_TOKEN:
                    raise ValueError(f"Secret '{SECRET_NAME}' does not contain 'fitcloud_api_token' key.")
            else:
                raise ValueError("Secret does not contain a SecretString.")
        except Exception as e:
            print(f"❌ Token retrieval failed: {e}")
            raise RuntimeError(f"Failed to retrieve API token: {e}")
    return FITCLOUD_API_TOKEN

def process_fitcloud_response(response_data, api_path):
    """FitCloud API 응답을 처리합니다."""
    # 응답이 리스트 형태일 경우 (예: /accounts)
    if isinstance(response_data, list):
        # /accounts의 경우 'data' 키 없이 바로 리스트를 반환하므로, 'accounts' 키로 래핑
        if api_path == '/accounts':
            return {"success": True, "accounts": response_data, "message": "계정 목록 조회 완료", "code": 200}
        else:
            return {"success": True, "data": response_data, "message": "조회 완료", "code": 200}
    
    # 응답이 딕셔너리 형태일 경우 (header/body 구조)
    if isinstance(response_data, dict):
        header = response_data.get('header', {})
        code = header.get('code')
        message = header.get('message', '')
        body = response_data.get('body', []) # 데이터가 없으면 빈 리스트

        if code == 200:
            # 비용 조회 API의 경우 body가 cost_items를 포함함
            if api_path.startswith('/costs/ondemand/'):
                return {"success": True, "cost_items": body, "message": message, "code": code}
            else: # 일반적인 body 데이터 (인보이스 등)
                return {"success": True, "data": body, "message": message, "code": code}
        elif code in [203, 204]: 
            # 데이터 없음, 그러나 성공적인 조회 응답으로 처리 (Bedrock Agent가 에러로 인식하지 않도록)
            if api_path.startswith('/costs/ondemand/'):
                return {"success": True, "cost_items": [], "message": message, "code": code}
            else:
                return {"success": True, "data": [], "message": message, "code": code} 
        else:
            # API 호출 자체는 성공했으나, FitCloud 내부 오류로 간주
            raise ValueError(f"FitCloud API error {code}: {message}")
    
    raise ValueError("Invalid response format from FitCloud API")

def format_account_list(accounts):
    """
    계정 목록을 예시2번(블록 형태)로 포맷팅하여 반환합니다.
    accounts: [
        {"accountName": "STARPASS", "accountId": "173511386181", "status": "ACTIVE"},
        ...
    ]
    """
    if not accounts:
        return "등록된 AWS 계정이 없습니다."
    lines = ["현재 FitCloud에 등록된 AWS 계정 목록입니다:\n"]
    for acc in accounts:
        lines.append(f"- **{acc.get('accountName', 'N/A')}**")
        lines.append(f"  - 계정 ID: {acc.get('accountId', 'N/A')}")
        lines.append(f"  - 상태: {'활성' if acc.get('status', '').upper() == 'ACTIVE' else '비활성'}\n")
    lines.append("특정 계정의 비용 정보나 사용량을 확인하고 싶으시면 언제든 말씀해 주세요!")
    return "\n".join(lines)

def create_bedrock_response(event, status_code=200, response_data=None, error_message=None):
    """Bedrock Agent에 맞는 응답 형식을 생성합니다."""
    action_group = event.get('actionGroup', 'unknown')
    api_path_from_event = event.get('apiPath', '') 
    http_method = event.get('httpMethod', 'POST')
    
    # 현재 날짜 정보를 sessionAttributes에 포함
    current_date_info = get_current_date_info()
    session_attributes = {
        'current_year': str(current_date_info['current_year']),
        'current_month': str(current_date_info['current_month']),
        'current_day': str(current_date_info['current_day']),
        'current_date': current_date_info['current_date_str'],
        'current_month_str': current_date_info['current_month_str']
    }
    
    final_data = {}

    if error_message:
        final_data = {
            "error": error_message,
            "success": False
        }
        status_code = 400 if status_code == 200 else status_code 
    else:
        final_data["success"] = response_data.get("success", True)
        final_data["message"] = response_data.get("message", "조회가 완료되었습니다.")
        
        # 'accounts' 또는 'cost_items'가 직접 최상위 레벨에 오도록 처리 (Bedrock 응답 가이드라인에 맞춤)
        # 스키마의 AccountListResponse 및 CostSummaryResponse에 맞춰 필드 매핑
        if "accounts" in response_data:
            clean_accounts = []
            for account in response_data["accounts"]:
                clean_accounts.append({
                    "accountName": account.get("accountName", "N/A"),
                    "accountId": account.get("accountId", "N/A"),
                    "email": account.get("email", "N/A"),
                    "status": account.get("status", "N/A")
                })
            final_data["accounts"] = clean_accounts
            final_data["total_count"] = len(clean_accounts)
            final_data["active_count"] = len([acc for acc in clean_accounts if acc.get('status') == 'ACTIVE'])
            # 자연어 message 추가 (예시2번 스타일)
            final_data["message"] = format_account_list(clean_accounts)

        elif "cost_items" in response_data:
            cost_items = []
            total_cost_sum_usd = 0.0 # USD 기준 총합
            is_daily = response_data.get("cost_type") == "daily"
            is_account_level = response_data.get("scope") == "account"
            for item in response_data["cost_items"]:
                try:
                    # USD 기준으로만 금액 집계
                    cost_usd = float(item.get('usageFee', 0.0))
                    cost_item = {
                        "serviceName": item.get('serviceName', '알 수 없음'),
                        "usageFeeUSD": round(cost_usd, 2) # 소수점 둘째 자리까지 반올림
                    }
                    # 날짜 필드 추가 (일별/월별 구분)
                    if is_daily:
                        cost_item["date"] = item.get('dailyDate')
                    else:
                        cost_item["date"] = item.get('monthlyDate')
                    # 계정별 조회인 경우 계정 정보 추가
                    if is_account_level:
                        cost_item["accountId"] = item.get('accountId', 'N/A')
                        cost_item["accountName"] = item.get('accountName', '알 수 없음')
                    cost_items.append(cost_item)
                    total_cost_sum_usd += cost_usd
                except (ValueError, TypeError) as e:
                    print(f"데이터 처리 오류 (비용 항목 스킵): {item} - {e}")
                    continue
            final_data["cost_type"] = response_data.get("cost_type")
            final_data["scope"] = response_data.get("scope")
            final_data["cost_items"] = cost_items
            final_data["total_cost_usd"] = round(total_cost_sum_usd, 2) # USD 총합
            final_data["item_count"] = len(cost_items)
            if not cost_items:
                final_data["message"] = f"조회된 비용 데이터가 없습니다."
                final_data["total_cost_usd"] = 0.0

        elif "data" in response_data: # 그 외 일반적인 데이터 리스트
            final_data["data"] = response_data["data"]
            if "count" in response_data: # 추가적인 카운트 필드
                final_data["count"] = response_data["count"]
            

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "apiPath": api_path_from_event, 
            "httpMethod": http_method,
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(final_data, ensure_ascii=False)
                }
            }
        },
        "sessionAttributes": session_attributes
    }

def determine_api_path(params):
    """
    파라미터 기반으로 올바른 API 경로 결정 (On-Demand 비용 조회용)
    우선순위: accountId 존재 여부 → 시간 단위 (daily/monthly)
    """
    
    # 1단계: accountId 존재 여부 먼저 확인
    has_account_id = 'accountId' in params and params['accountId'] and str(params['accountId']).strip() != '' and str(params['accountId']).strip().lower() != 'none'
    
    # 2단계: 시간 단위 확인 (날짜 형식으로 판단)
    # smart_date_correction에서 이미 from/to가 채워졌다고 가정
    date_format = None
    if 'from' in params and params['from']: 
        from_param = str(params['from'])
        if len(from_param) == 8:  # YYYYMMDD
            date_format = 'daily'
        elif len(from_param) == 6:  # YYYYMM
            date_format = 'monthly'
    
    print(f"🔍 API 경로 결정 로직:")
    print(f"  - accountId 존재: {has_account_id} (값: '{params.get('accountId')}')")
    print(f"  - from 값: '{params.get('from', '없음')}'")
    print(f"  - 판단된 날짜 형식: {date_format}")
    
    # 3단계: API 경로 결정
    if has_account_id:
        if date_format == 'daily':
            return '/costs/ondemand/account/daily'
        elif date_format == 'monthly':
            return '/costs/ondemand/account/monthly'
        else:
            # 날짜 형식을 알 수 없으면 기본적으로 '일별'로 가정
            print("❗ accountId는 있으나 날짜 형식 미정. 계정별 일별로 기본값 설정.")
            return '/costs/ondemand/account/daily'
    else: # 법인 전체 조회
        if date_format == 'daily':
            return '/costs/ondemand/corp/daily'
        elif date_format == 'monthly':
            return '/costs/ondemand/corp/monthly'
        else:
            # 날짜 형식을 알 수 없으면 기본적으로 '일별'로 가정
            print("❗ accountId 없고 날짜 형식 미정. 법인 일별로 기본값 설정.")
            return '/costs/ondemand/corp/daily'


def extract_parameters(event):
    """이벤트에서 파라미터를 추출합니다."""
    params = {}
    session_current_year = None
    # Query Parameters (OpenAPI path parameters)
    if 'parameters' in event:
        for param in event['parameters']:
            params[param['name']] = param['value']
    # Request Body Parameters (from Bedrock Agent)
    if 'requestBody' in event and 'content' in event['requestBody']:
        content = event['requestBody']['content']
        # application/x-www-form-urlencoded 처리
        if 'application/x-www-form-urlencoded' in content:
            body_content = content['application/x-www-form-urlencoded']
            if 'body' in body_content: # 기본 바디 형태 (단일 문자열)
                body_str = body_content['body']
                from urllib.parse import parse_qs
                parsed_body = parse_qs(body_str)
                for key, value_list in parsed_body.items():
                    if value_list:
                        params[key] = value_list[0]
            elif 'properties' in body_content: # 스키마의 properties 형태
                for prop_data in body_content['properties']:
                    params[prop_data['name']] = prop_data['value']
        # application/json 처리
        elif 'application/json' in content:
            body_str = content['application/json'].get('body')
            if body_str:
                try:
                    json_body = json.loads(body_str)
                    params.update(json_body)
                except json.JSONDecodeError:
                    print(f"JSON body 파싱 실패: {body_str[:100]}...")
                    pass
    # 세션 속성에서 날짜 정보 가져오기 (Agent가 전달했다면)
    if 'sessionAttributes' in event:
        session_attrs = event['sessionAttributes']
        if 'current_year' in session_attrs:
            session_current_year = str(session_attrs['current_year'])
            print(f"DEBUG: Session Attributes에서 current_year 감지: {session_current_year}")
    # 월만 입력된 경우 보정 (current_year 우선 적용)
    for k, v in list(params.items()):
        if k in ['from', 'to', 'billingPeriod', 'beginDate', 'endDate']:
            v_str = str(v)
            if (len(v_str) == 1 or (len(v_str) == 2 and v_str.isdigit())) and session_current_year:
                # 월만 입력된 경우
                params[k] = f"{session_current_year}{v_str.zfill(2)}"
                print(f"[extract_parameters] 월만 입력된 {k} → {params[k]} (sessionAttributes.current_year 적용)")
    return params

def lambda_handler(event, context):
    print(f"--- 슈퍼바이저 API 호출 시작 (Bedrock Agent Event) ---")
    print(f"수신된 이벤트: {json.dumps(event, indent=2, ensure_ascii=False)}")

    try:
        # 기본 이벤트 형식 검증
        if 'messageVersion' not in event or 'actionGroup' not in event:
            return create_bedrock_response(event, 400, error_message="Invalid event format from Bedrock Agent.")

        api_path_from_event = event.get('apiPath') # Agent가 호출하려는 API 경로
        
        if not api_path_from_event:
            return create_bedrock_response(event, 400, error_message="API path missing in event payload.")

        # 파라미터 추출
        params = extract_parameters(event)
        print(f"📝 원본 추출 파라미터: {params}")

        # ✨ 날짜 보정 로직 적용 ✨
        # 'from' 또는 'to' 파라미터가 없으면, smart_date_correction 내부에서 오늘 날짜로 기본값 설정 시도
        params = smart_date_correction(params)
        print(f"📝 보정 후 파라미터: {params}")
        
        date_warnings = validate_date_logic(params) # 보정된 파라미터로 다시 검증
        
        # 날짜 유효성 검증에서 경고가 발생하면 클라이언트에게 오류 응답을 반환합니다.
        if date_warnings:
            print(f"DEBUG: 날짜 유효성 검증 경고: {date_warnings}")
            # 400 Bad Request로 응답하여 Agent가 재요청하거나 사용자에게 알리도록 함
            return create_bedrock_response(
                event, 400, 
                error_message=f"날짜 오류: {'; '.join(date_warnings)}. 유효한 날짜 또는 기간을 입력해주세요."
            )
        
        print(f"📝 최종 확인 파라미터: {params}")
        # ✨ 날짜 보정 로직 적용 끝 ✨

        # API 경로 결정 (모든 FitCloud API 경로 지원)
        target_api_path = None
        if api_path_from_event == '/accounts':
            target_api_path = '/accounts'
        elif api_path_from_event.startswith('/costs/ondemand/'):
            target_api_path = determine_api_path(params)
            print(f"DEBUG: 비용 API 경로 동적 결정: {api_path_from_event} -> {target_api_path}")
        elif api_path_from_event.startswith('/invoice/') or api_path_from_event.startswith('/usage/'):
            # 청구서 및 사용량 API는 람다2에서 처리하므로 그대로 전달
            target_api_path = api_path_from_event
            print(f"DEBUG: 청구서/사용량 API 경로: {api_path_from_event}")
        else:
            return create_bedrock_response(event, 404, error_message=f"지원하지 않는 엔드포인트: {api_path_from_event}")

        # 토큰 획득
        try:
            current_token = get_fitcloud_token()
            print("✅ FitCloud API 토큰 획득 성공")
        except RuntimeError as e:
            return create_bedrock_response(event, 401, error_message=f"FitCloud API 인증 실패: {str(e)}")

        # 세션 및 헤더 설정
        session = create_retry_session()
        headers = {
            'Authorization': f'Bearer {current_token}',
            'Content-Type': 'application/x-www-form-urlencoded', # FitCloud API가 form-urlencoded를 요구할 경우
            'User-Agent': 'FitCloud-Lambda/1.0'
        }

        # API 호출 로직 (target_api_path 기반으로 분기)
        response = None
        
        # 공통 파라미터 체크 함수 (필수 파라미터 누락 여부 확인)
        def check_and_prepare_data(required_params_list, optional_params_list=[]):
            data = {}
            for p in required_params_list:
                # None, 빈 문자열, "None" 문자열 모두 유효하지 않다고 판단
                if p not in params or params[p] is None or str(params[p]).strip() == '' or str(params[p]).strip().lower() == 'none':
                    raise ValueError(f"필수 파라미터 누락 또는 유효하지 않음: '{p}'")
                data[p] = params[p]
            for p in optional_params_list:
                if p in params and params[p] is not None and str(params[p]).strip() != '' and str(params[p]).strip().lower() != 'none':
                    data[p] = params[p]
            return data

        print(f"📞 FitCloud API 호출 준비: {FITCLOUD_BASE_URL}{target_api_path}")
        if target_api_path == '/accounts':
            print("  - 작업: 계정 목록 조회")
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, timeout=30)
            
        elif target_api_path == '/costs/ondemand/corp/monthly':
            print("  - 작업: 법인 월별 온디맨드 비용 조회")
            api_data = check_and_prepare_data(['from', 'to'])
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path == '/costs/ondemand/account/monthly':
            print("  - 작업: 계정 월별 온디맨드 비용 조회")
            api_data = check_and_prepare_data(['from', 'to', 'accountId'])
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path == '/costs/ondemand/corp/daily':
            print("  - 작업: 법인 일별 온디맨드 비용 조회")
            api_data = check_and_prepare_data(['from', 'to'])
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path == '/costs/ondemand/account/daily':
            print("  - 작업: 계정 일별 온디맨드 비용 조회")
            api_data = check_and_prepare_data(['from', 'to', 'accountId'])
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path.startswith('/invoice/') or target_api_path.startswith('/usage/'):
            print(f"  - 작업: 청구서/사용량 API 호출 ({target_api_path})")
            # 청구서 및 사용량 API는 람다2에서 처리하므로 파라미터만 전달
            api_data = check_and_prepare_data(['billingPeriod'] if 'billingPeriod' in params else ['from', 'to'])
            if 'accountId' in params:
                api_data['accountId'] = params['accountId']
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        else:
            return create_bedrock_response(event, 404, error_message=f"처리할 수 없는 API 경로: {target_api_path}")

        # 응답 처리
        print(f"API 응답 HTTP 상태 코드: {response.status_code}")
        response.raise_for_status() # HTTP 오류가 발생하면 requests.exceptions.HTTPError 예외 발생
        
        raw_data = response.json()
        print("--- Raw API Response Start ---")
        print(json.dumps(raw_data, indent=2, ensure_ascii=False)) 
        print("--- Raw API Response End ---")

        processed_data_wrapper = process_fitcloud_response(raw_data, target_api_path) 
        
        print(f"✅ Bedrock Agent 응답 생성 중...")
        # create_bedrock_response에서 response_data와 target_api_path를 활용하여 final_data 구성
        return create_bedrock_response(event, 200, processed_data_wrapper)

    except ValueError as e:
        # 주로 check_and_prepare_data에서 발생, 잘못된 파라미터나 형식
        error_msg = f"잘못된 요청 파라미터 또는 형식: {str(e)}"
        print(f"❌ {error_msg}")
        return create_bedrock_response(event, 400, error_message=error_msg)
    except requests.exceptions.HTTPError as e:
        # 외부 FitCloud API 호출 중 HTTP 오류 (4xx, 5xx)
        status_code = e.response.status_code if e.response is not None else 500
        response_text = e.response.text[:200] if e.response and e.response.text else "응답 내용 없음"
        error_msg = f"FitCloud API 통신 오류: {status_code} - {response_text}..."
        print(f"❌ {error_msg}")
        return create_bedrock_response(event, status_code, error_message=error_msg)
    except requests.exceptions.ConnectionError as e:
        # 네트워크 연결 오류
        error_msg = f"FitCloud API 연결 오류: {str(e)}. 네트워크 상태를 확인해주세요."
        print(f"❌ {error_msg}")
        return create_bedrock_response(event, 503, error_message=error_msg)
    except requests.exceptions.Timeout as e:
        # API 호출 타임아웃
        error_msg = f"FitCloud API 응답 시간 초과: {str(e)}. 잠시 후 다시 시도해주세요."
        print(f"❌ {error_msg}")
        return create_bedrock_response(event, 504, error_message=error_msg)
    except Exception as e:
        # 예상치 못한 모든 기타 오류
        error_msg = f"시스템 내부 오류가 발생했습니다: {type(e).__name__} - {str(e)}"
        print(f"💥 {error_msg}")
        # Unhandled 오류 메시지에 상세 정보 포함
        return create_bedrock_response(event, 500, error_message=error_msg)