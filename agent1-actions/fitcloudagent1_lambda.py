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
    utc_now = datetime.utcnow()
    tz = pytz.timezone('Asia/Seoul')
    utc_with_tz = pytz.utc.localize(utc_now)
    now = utc_with_tz.astimezone(tz)
    
    return {
        'current_year': now.year,
        'current_month': now.month,
        'current_day': now.day,
        'current_datetime': now,
        'current_date_str': now.strftime('%Y%m%d'),
        'current_month_str': now.strftime('%Y%m'),
        'utc_time': utc_now.isoformat(),
        'kst_time': now.isoformat()
    }

def smart_date_correction(params):
    """
    사용자 의도에 맞게 날짜 파라미터를 보정합니다.
    """
    current_info = get_current_date_info()
    current_year = current_info['current_year']
    
    corrected_params = params.copy()
    
    # 'from' 또는 'to' 파라미터가 없는 경우, 현재 날짜를 기본값으로 설정
    if 'from' not in corrected_params and 'to' not in corrected_params:
        if 'billingPeriod' in corrected_params:
            print(f"📅 billingPeriod 존재: {corrected_params['billingPeriod']}")
        else:
            today_str = f"{current_year}{current_info['current_month']:02d}{current_info['current_day']:02d}"
            corrected_params['from'] = today_str
            corrected_params['to'] = today_str
            print(f"📅 기본값 설정: from={today_str}, to={today_str}")

    for param_name in ['from', 'to']:
        original_value = str(corrected_params.get(param_name, ''))
        
        if not original_value.strip():
            continue

        # 월만 입력된 경우(예: '5', '05')
        if len(original_value) == 1 or (len(original_value) == 2 and original_value.isdigit()):
            month_str = original_value.zfill(2)
            yyyymm = f"{current_year}{month_str}"
            corrected_params[param_name] = yyyymm
            print(f"📅 {param_name} 보정: {original_value} → {yyyymm}")
            continue

        # MMDD 형태 (예: '0603')
        if len(original_value) == 4 and original_value.isdigit():
            test_date_str = str(current_year) + original_value
            try:
                datetime.strptime(test_date_str, '%Y%m%d')
                corrected_params[param_name] = test_date_str
                print(f"📅 {param_name} 보정: {original_value} → {test_date_str}")
                continue
            except ValueError:
                pass

        # YYYYMMDD 또는 YYYYMM 형식에서 연도 보정
        if len(original_value) == 8 or len(original_value) == 6:
            year_part = original_value[:4]
            suffix_part = original_value[4:]
            try:
                # 현재 연도보다 5년 이상 과거인 경우에만 연도 보정
                if int(year_part) < current_year - 5 and int(year_part) >= 2020:
                    corrected_value = str(current_year) + suffix_part
                    if len(corrected_value) == 8:
                        datetime.strptime(corrected_value, '%Y%m%d')
                    elif len(corrected_value) == 6:
                        datetime.strptime(corrected_value + '01', '%Y%m%d')
                    corrected_params[param_name] = corrected_value
                    print(f"📅 {param_name} 연도 보정: {original_value} → {corrected_value}")
            except ValueError:
                pass

    return corrected_params

def validate_date_logic(params, api_path=None):
    """
    보정된 날짜의 논리적 타당성을 검증합니다.
    API 경로에 따라 필요한 파라미터를 정확히 검증합니다.
    """
    current_info = get_current_date_info()
    current_date_only = current_info['current_datetime'].date() 

    warnings = []
    
    # API 경로별 필수 파라미터 정의
    api_requirements = {
        # 람다1 (슈퍼바이저) - 비용 조회 API
        '/costs/ondemand/corp/monthly': {'required': ['from', 'to'], 'format': 'YYYYMM'},
        '/costs/ondemand/account/monthly': {'required': ['from', 'to', 'accountId'], 'format': 'YYYYMM'},
        '/costs/ondemand/corp/daily': {'required': ['from', 'to'], 'format': 'YYYYMMDD'},
        '/costs/ondemand/account/daily': {'required': ['from', 'to', 'accountId'], 'format': 'YYYYMMDD'},
        
        # 람다2 (에이전트2) - 청구서/사용량 API
        '/invoice/corp/monthly': {'required': ['billingPeriod'], 'format': 'YYYYMM'},
        '/invoice/account/monthly': {'required': ['billingPeriod'], 'format': 'YYYYMM'},
        '/usage/ondemand/monthly': {'required': ['from', 'to'], 'format': 'YYYYMM'},
        '/usage/ondemand/daily': {'required': ['from', 'to'], 'format': 'YYYYMMDD'},
        '/usage/ondemand/tags': {'required': ['beginDate', 'endDate'], 'format': 'YYYYMMDD'},
    }
    
    # API 경로가 지정된 경우 해당 API의 필수 파라미터 검증
    if api_path and api_path in api_requirements:
        requirements = api_requirements[api_path]
        required_params = requirements['required']
        expected_format = requirements['format']
        
        # 필수 파라미터 존재 여부 확인
        missing_params = []
        for param in required_params:
            if param not in params or not str(params[param]).strip():
                missing_params.append(param)
        
        if missing_params:
            warnings.append(f"필수 파라미터가 누락되었습니다: {', '.join(missing_params)}")
            return warnings
        
        # 파라미터 형식 검증
        for param in required_params:
            param_value = str(params[param])
            if expected_format == 'YYYYMM' and not (len(param_value) == 6 and param_value.isdigit()):
                warnings.append(f"'{param}' 파라미터는 YYYYMM 형식(6자리 숫자)이어야 합니다: {param_value}")
            elif expected_format == 'YYYYMMDD' and not (len(param_value) == 8 and param_value.isdigit()):
                warnings.append(f"'{param}' 파라미터는 YYYYMMDD 형식(8자리 숫자)이어야 합니다: {param_value}")
    
    # billingPeriod 검증 (청구서 API용)
    if 'billingPeriod' in params:
        billing_period = str(params['billingPeriod'])
        if len(billing_period) == 6:  # YYYYMM 형식
            try:
                year = int(billing_period[:4])
                month = int(billing_period[4:])
                current_year = current_info['current_year']
                current_month = current_info['current_month']
                
                # 현재 월보다 이후 월만 미래로 간주 (같은 연도의 과거 월은 허용)
                is_future_month = (year > current_year) or \
                                (year == current_year and month > current_month)
                
                if is_future_month:
                    warnings.append(f"요청하신 월이 미래입니다: {billing_period} (현재: {current_year}{current_month:02d})")
                    
            except ValueError as e:
                warnings.append(f"billingPeriod 파싱 오류: {e}. 유효한 월 형식(YYYYMM)을 입력해주세요.")
    
    # from/to 파라미터 검증 (비용/사용량 API용)
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
                
                # 현재 연도와 월을 기준으로 미래인지 판단
                current_year = current_info['current_year']
                current_month = current_info['current_month']
                
                # 현재 월보다 이후 월만 미래로 간주 (같은 연도의 과거 월은 허용)
                is_from_future_month = (req_from_year > current_year) or \
                                     (req_from_year == current_year and req_from_month > current_month)
                is_to_future_month = (req_to_year > current_year) or \
                                   (req_to_year == current_year and req_to_month > current_month)
                
                # 미래 월인 경우에만 경고
                if is_from_future_month or is_to_future_month:
                    warnings.append(f"요청하신 월이 미래입니다: {from_str} - {to_str} (현재: {current_year}{current_month:02d})")
                    
        except ValueError as e:
            warnings.append(f"날짜 파싱 오류: {e}. 유효한 날짜 형식을 입력해주세요.")
    
    # beginDate/endDate 파라미터 검증 (태그별 사용량 API용)
    if 'beginDate' in params and 'endDate' in params:
        begin_str = str(params['beginDate'])
        end_str = str(params['endDate'])
        
        try:
            if len(begin_str) == 8 and len(end_str) == 8:  # YYYYMMDD 형식
                begin_dt_obj = datetime.strptime(begin_str, '%Y%m%d').date()
                end_dt_obj = datetime.strptime(end_str, '%Y%m%d').date()
                
                # 조회 기간 시작일이 종료일보다 늦을 경우
                if begin_dt_obj > end_dt_obj:
                    warnings.append("조회 시작일이 종료일보다 늦습니다.")

                # 시작 날짜 또는 종료 날짜가 오늘보다 미래인 경우
                if begin_dt_obj > current_date_only or end_dt_obj > current_date_only:
                    warnings.append(f"요청하신 날짜가 미래입니다: {begin_str} - {end_str}")
            else:
                warnings.append("날짜 형식이 올바르지 않습니다 (YYYYMMDD).")
                return warnings
                    
        except ValueError as e:
            warnings.append(f"날짜 파싱 오류: {e}. 유효한 날짜 형식을 입력해주세요.")

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
    # 응답이 리스트 형태일 경우 (예: /s)
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
    
    # 계정 정보를 sessionAttributes에 추가 (계정 목록 조회 시)
    if response_data and "accounts" in response_data:
        accounts_info = []
        for account in response_data["accounts"]:
            accounts_info.append({
                "accountName": account.get("accountName", "N/A"),
                "accountId": account.get("accountId", "N/A")
            })
        session_attributes['available_accounts'] = json.dumps(accounts_info, ensure_ascii=False)
        print(f"📋 계정 정보를 sessionAttributes에 추가: {len(accounts_info)}개 계정")
    
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
    """
    has_billing_period = 'billingPeriod' in params and params['billingPeriod'] and str(params['billingPeriod']).strip() != '' and str(params['billingPeriod']).strip().lower() != 'none'
    has_account_id = 'accountId' in params and params['accountId'] and str(params['accountId']).strip() != '' and str(params['accountId']).strip().lower() != 'none'
    
    date_format = None
    if 'from' in params and params['from']: 
        from_param = str(params['from'])
        if len(from_param) == 8:
            date_format = 'daily'
        elif len(from_param) == 6:
            date_format = 'monthly'
    
    print(f"🔍 API 경로 결정: billingPeriod={has_billing_period}, accountId={has_account_id}, format={date_format}")
    
    if has_billing_period:
        if has_account_id:
            print(f"  → 계정별 월별 API")
            return '/costs/ondemand/account/monthly'
        else:
            print(f"  → 법인 월별 API")
            return '/costs/ondemand/corp/monthly'
    
    if has_account_id:
        if date_format == 'daily':
            return '/costs/ondemand/account/daily'
        elif date_format == 'monthly':
            return '/costs/ondemand/account/monthly'
        else:
            print(f"  → 계정별 일별 API (기본값)")
            return '/costs/ondemand/account/daily'
    else:
        if date_format == 'daily':
            return '/costs/ondemand/corp/daily'
        elif date_format == 'monthly':
            return '/costs/ondemand/corp/monthly'
        else:
            print(f"  → 법인 일별 API (기본값)")
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
        if 'application/x-www-form-urlencoded' in content:
            body_content = content['application/x-www-form-urlencoded']
            if 'body' in body_content:
                body_str = body_content['body']
                from urllib.parse import parse_qs
                parsed_body = parse_qs(body_str)
                for key, value_list in parsed_body.items():
                    if value_list:
                        params[key] = value_list[0]
            elif 'properties' in body_content:
                for prop_data in body_content['properties']:
                    params[prop_data['name']] = prop_data['value']
        elif 'application/json' in content:
            body_str = content['application/json'].get('body')
            if body_str:
                try:
                    json_body = json.loads(body_str)
                    params.update(json_body)
                except json.JSONDecodeError:
                    pass
    
    # 세션 속성에서 날짜 정보 가져오기
    if 'sessionAttributes' in event:
        session_attrs = event['sessionAttributes']
        if 'current_year' in session_attrs:
            session_current_year = str(session_attrs['current_year'])
    
    # 현재 연도로 보정 (세션 연도가 잘못되어 있으면 현재 연도 사용)
    current_info = get_current_date_info()
    real_current_year = str(current_info['current_year'])
    if not session_current_year or session_current_year != real_current_year:
        session_current_year = real_current_year
        print(f"📅 세션 연도 보정: {session_current_year} → {real_current_year}")
    
    # inputText에서 월 정보 추출
    input_text = event.get('inputText', '')
    import re
    month_match = re.search(r'([0-9]{1,2})월', input_text)
    if month_match and not params.get('billingPeriod'):
        month_str = month_match.group(1).zfill(2)
        params['billingPeriod'] = f"{session_current_year}{month_str}"
        print(f"📅 inputText에서 월 추출: {params['billingPeriod']}")
    
    # 월만 입력된 경우 보정
    for k, v in list(params.items()):
        if k in ['from', 'to', 'billingPeriod', 'beginDate', 'endDate']:
            v_str = str(v)
            if (len(v_str) == 1 or (len(v_str) == 2 and v_str.isdigit())) and session_current_year:
                params[k] = f"{session_current_year}{v_str.zfill(2)}"
                print(f"📅 월 보정: {k}={v} → {params[k]}")
    
    # billingPeriod 자동 생성
    if not params.get('billingPeriod') and params.get('from') and len(str(params['from'])) >= 6:
        params['billingPeriod'] = str(params['from'])[:6]
    if not params.get('billingPeriodDaily') and params.get('from') and len(str(params['from'])) == 8:
        params['billingPeriodDaily'] = str(params['from'])
    
    return params

def lambda_handler(event, context):
    print(f"🚀 Lambda 1 시작: {event.get('apiPath', 'N/A')}")
    print(f"[DEBUG] Raw event: {json.dumps(event, ensure_ascii=False)[:1000]}")  # 이벤트 전체(1000자 제한) 로그

    api_path_from_event = event.get('apiPath')
    if api_path_from_event == '/accounts':
        # 계정 목록 조회는 파라미터/날짜/계정 검증 없이 바로 호출
        try:
            current_token = get_fitcloud_token()
            print("[DEBUG] FitCloud API 토큰 획득 성공 (/accounts)")
            session = create_retry_session()
            headers = {
                'Authorization': f'Bearer {current_token}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'FitCloud-Lambda/1.0'
            }
            print("[DEBUG] /accounts API 호출")
            response = session.post(f'{FITCLOUD_BASE_URL}/accounts', headers=headers, timeout=30)
            response.raise_for_status()
            raw_data = response.json()
            print(f"[DEBUG] /accounts API 응답: {json.dumps(raw_data, ensure_ascii=False)[:1000]}")
            processed_data_wrapper = process_fitcloud_response(raw_data, '/accounts')
            print(f"[DEBUG] /accounts 최종 응답 데이터: {processed_data_wrapper}")
            return create_bedrock_response(event, 200, processed_data_wrapper)
        except Exception as e:
            error_msg = f"계정 목록 조회 중 오류: {type(e).__name__} - {str(e)}"
            print(f"[ERROR] {error_msg}")
            return create_bedrock_response(event, 500, error_message=error_msg)

    try:
        if 'messageVersion' not in event or 'actionGroup' not in event:
            print("[ERROR] Bedrock Agent에서 온 이벤트 포맷 오류")
            return create_bedrock_response(event, 400, error_message="Invalid event format from Bedrock Agent.")

        # 파라미터 추출
        params = extract_parameters(event)
        print(f"[DEBUG] 추출된 파라미터: {params}")

        # 날짜 보정
        params = smart_date_correction(params)
        print(f"[DEBUG] 날짜 보정 후 파라미터: {params}")

        # 사용자 의도 파악 (지침서 기준)
        input_text = event.get('inputText', '').lower()
        is_invoice_request = any(k in input_text for k in ['청구서', 'invoice', '인보이스', '최종 청구 금액', '실제 결제 금액', '실제 지불 금액'])
        is_usage_request = any(k in input_text for k in ['순수 온디맨드', '순수 사용량', '할인 미적용', 'ri/sp 제외', '원가 기준', '할인 금액이 포함되지 않은', '할인 전 금액', '정가 기준', 'pure usage'])
        has_account = any(k in input_text for k in ['계정', 'account', '개발계정', 'dev'])
        print(f"[DEBUG] 사용자 의도 분석: is_invoice={is_invoice_request}, is_usage={is_usage_request}, has_account={has_account}")

        # 1. 청구서/인보이스 요청이면 람다2로 위임
        if is_invoice_request:
            if has_account:
                target_api_path = '/invoice/account/monthly'
            else:
                target_api_path = '/invoice/corp/monthly'
            print(f"[DEBUG] 청구서 요청 → {target_api_path}")
        # 2. 순수 온디맨드/순수 사용량/할인 미적용 요청이면 람다2로 위임
        elif is_usage_request:
            if has_account:
                target_api_path = '/usage/ondemand/account/monthly'
            else:
                target_api_path = '/usage/ondemand/corp/monthly'
            print(f"[DEBUG] 순수 사용량 요청 → {target_api_path}")
        # 3. 그 외는 costs API(람다1)에서 직접 처리
        else:
            if has_account:
                target_api_path = '/costs/ondemand/account/monthly'
            else:
                target_api_path = '/costs/ondemand/corp/monthly'
            print(f"[DEBUG] 일반 비용/사용량 요청 → {target_api_path}")
            # costs API에서만 billingPeriod → from/to 변환
            if 'billingPeriod' in params and not ('from' in params and 'to' in params):
                billing_period = str(params['billingPeriod'])
                if len(billing_period) == 6:
                    params['from'] = billing_period
                    params['to'] = billing_period
                    print(f"[DEBUG] billingPeriod 변환: {billing_period} → from/to (비용 API용)")

        # 날짜 검증
        date_warnings = validate_date_logic(params, target_api_path)
        if date_warnings:
            print(f"[ERROR] 날짜 검증 실패: {date_warnings}")
            return create_bedrock_response(
                event, 400, 
                error_message=f"날짜 오류: {'; '.join(date_warnings)}. 유효한 날짜 또는 기간을 입력해주세요."
            )

        # 이하 기존 API 호출 분기 및 응답 처리 로직은 target_api_path 기준으로 그대로 유지
        # 토큰 획득
        try:
            current_token = get_fitcloud_token()
            print("[DEBUG] FitCloud API 토큰 획득 성공")
        except RuntimeError as e:
            print(f"[ERROR] 토큰 획득 실패: {e}")
            return create_bedrock_response(event, 401, error_message=f"FitCloud API 인증 실패: {str(e)}")

        # 세션 및 헤더 설정
        session = create_retry_session()
        headers = {
            'Authorization': f'Bearer {current_token}',
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'FitCloud-Lambda/1.0'
        }

        # API 호출 로직 (target_api_path 기반으로 분기)
        response = None
        print(f"[DEBUG] API 호출 준비: {target_api_path}, 파라미터: {params}")
        if target_api_path == '/accounts':
            print("  - 계정 목록 조회")
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, timeout=30)
            
        elif target_api_path == '/costs/ondemand/corp/monthly':
            print("  - 법인 월별 비용 조회")
            api_data = check_and_prepare_data(['from', 'to'])
            if 'billingPeriod' in params:
                api_data['billingPeriod'] = params['billingPeriod']
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path == '/costs/ondemand/account/monthly':
            print("  - 계정 월별 비용 조회")
            api_data = check_and_prepare_data(['from', 'to', 'accountId'])
            if 'billingPeriod' in params:
                api_data['billingPeriod'] = params['billingPeriod']
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path == '/costs/ondemand/corp/daily':
            print("  - 법인 일별 비용 조회")
            api_data = check_and_prepare_data(['from', 'to'])
            if 'billingPeriodDaily' in params:
                api_data['billingPeriodDaily'] = params['billingPeriodDaily']
            if 'serviceName' in params:
                api_data['serviceName'] = params['serviceName']
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path == '/costs/ondemand/account/daily':
            print("  - 계정 일별 비용 조회")
            api_data = check_and_prepare_data(['from', 'to', 'accountId'])
            if 'billingPeriodDaily' in params:
                api_data['billingPeriodDaily'] = params['billingPeriodDaily']
            if 'serviceName' in params:
                api_data['serviceName'] = params['serviceName']
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path.startswith('/invoice/'):
            print(f"  - 청구서 API 호출")
            api_data = check_and_prepare_data(['billingPeriod'])
            
            if 'accountId' in params:
                api_data['accountId'] = params['accountId']
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        elif target_api_path.startswith('/usage/'):
            print(f"  - 사용량 API 호출")
            if 'billingPeriod' in params:
                billing_period = params['billingPeriod']
                if len(billing_period) == 6:
                    year = billing_period[:4]
                    month = billing_period[4:]
                    from_date = f"{year}{month}01"
                    import calendar
                    last_day = calendar.monthrange(int(year), int(month))[1]
                    to_date = f"{year}{month}{last_day:02d}"
                    api_data = {'from': from_date, 'to': to_date}
                else:
                    api_data = check_and_prepare_data(['billingPeriod'])
            elif 'beginDate' in params and 'endDate' in params:
                api_data = check_and_prepare_data(['beginDate', 'endDate'])
            else:
                api_data = check_and_prepare_data(['from', 'to'])
            
            if 'accountId' in params:
                api_data['accountId'] = params['accountId']
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, data=api_data, timeout=30)
            
        else:
            return create_bedrock_response(event, 404, error_message=f"처리할 수 없는 API 경로: {target_api_path}")

        # 응답 처리
        try:
            response.raise_for_status()
        except Exception as e:
            print(f"[ERROR] API HTTP 오류: {e}, 응답: {getattr(response, 'text', None)}")
            raise
        try:
            raw_data = response.json()
        except Exception as e:
            print(f"[ERROR] API 응답 JSON 파싱 오류: {e}, 응답: {getattr(response, 'text', None)}")
            raise
        print(f"[DEBUG] API 응답 수신: {json.dumps(raw_data, ensure_ascii=False)[:1000]}")

        processed_data_wrapper = process_fitcloud_response(raw_data, target_api_path) 
        print(f"[DEBUG] 최종 응답 데이터: {processed_data_wrapper}")
        return create_bedrock_response(event, 200, processed_data_wrapper)

    except ValueError as e:
        error_msg = f"잘못된 요청 파라미터 또는 형식: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return create_bedrock_response(event, 400, error_message=error_msg)
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 500
        response_text = e.response.text[:200] if e.response and e.response.text else "응답 내용 없음"
        error_msg = f"FitCloud API 통신 오류: {status_code} - {response_text}..."
        print(f"[ERROR] {error_msg}")
        return create_bedrock_response(event, status_code, error_message=error_msg)
    except requests.exceptions.ConnectionError as e:
        error_msg = f"FitCloud API 연결 오류: {str(e)}. 네트워크 상태를 확인해주세요."
        print(f"[ERROR] {error_msg}")
        return create_bedrock_response(event, 503, error_message=error_msg)
    except requests.exceptions.Timeout as e:
        error_msg = f"FitCloud API 응답 시간 초과: {str(e)}. 잠시 후 다시 시도해주세요."
        print(f"[ERROR] {error_msg}")
        return create_bedrock_response(event, 504, error_message=error_msg)
    except Exception as e:
        error_msg = f"시스템 내부 오류가 발생했습니다: {type(e).__name__} - {str(e)}"
        print(f"[ERROR] {error_msg}")
        return create_bedrock_response(event, 500, error_message=error_msg)