import json
import os
import requests
import boto3
from urllib.parse import parse_qs
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from datetime import datetime, timedelta, date 
import pytz # 시간대 처리를 위해 pytz 라이브러리 추가

# 환경 변수에서 FitCloud API 기본 URL 및 Secrets Manager 보안 암호 가져오기
FITCLOUD_BASE_URL = os.environ.get('FITCLOUD_BASE_URL', 'https://aws-dev.fitcloud.co.kr/api/v1')
SECRET_NAME = os.environ.get('FITCLOUD_API_SECRET_NAME', 'dev-FitCloud/ApiToken') # Secrets Manager의 실제 이름으로 변경 필요

# Secrets Manager 클라이언트 초기화
secrets_client = boto3.client('secretsmanager')

# 토큰 캐싱을 위한 전역 변수
FITCLOUD_API_TOKEN = None

# Custom JSON Serializer 함수 추가
def custom_json_serializer(obj):
    """
    JSON 직렬화 시 datetime, date 객체를 ISO 8601 문자열로 변환합니다.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

def prepare_form_data(data_dict):
    """
    requests.post의 'files' 파라미터에 넘길 수 있도록 딕셔너리를 변환합니다.
    이를 통해 'multipart/form-data' 요청을 생성합니다.
    """
    prepared_data = {}
    for key, value in data_dict.items():
        # 값을 문자열로 변환하고 (None, value_str) 형태로 튜플 생성
        # 이는 requests가 해당 필드를 파일이 아닌 일반 폼 필드로 인식하게 합니다.
        prepared_data[key] = (None, str(value))
    return prepared_data

def get_current_date_info():
    """현재 날짜 정보를 반환합니다 (KST 기준)."""
    # 디버깅을 위한 상세 로깅 추가
    import os
    
    print(f"🔍 시간대 디버깅 정보:")
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
        print(f"⚠️ 경고: 시간대 계산 방법 간 차이 발견!")
        print(f"  - 방법 1: {now_method1.date()}")
        print(f"  - 방법 2: {now_method2.date()}")
    
    current_info = {
        'current_year': now.year,
        'current_month': now.month,
        'current_day': now.day,
        'current_datetime': now, # 시간대 정보 포함된 datetime 객체
        'current_date_str': now.strftime('%Y%m%d'),  # YYYYMMDD 형식
        'current_month_str': now.strftime('%Y%m'),   # YYYYMM 형식
        'timezone': str(now.tzinfo),
        'utc_time': utc_now.isoformat(),  # UTC 시간도 포함
        'kst_time': now.isoformat()       # KST 시간도 포함
    }
    
    print(f"🕐 최종 현재 시간 정보:")
    print(f"  - 현재 날짜/시간: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  - 연도: {current_info['current_year']}")
    print(f"  - 월: {current_info['current_month']}")
    print(f"  - 일: {current_info['current_day']}")
    print(f"  - {current_info['current_date_str']} 형식: {current_info['current_date_str']}")
    print(f"  - {current_info['current_month_str']} 형식: {current_info['current_month_str']}")
    print(f"  - 시간대: {current_info['timezone']}")
    
    return current_info

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
            print(f"토큰 획득 실패: {e}")
            raise RuntimeError(f"Failed to retrieve API token: {e}")
    return FITCLOUD_API_TOKEN

def process_fitcloud_response(response_data):
    """FitCloud API 응답을 처리합니다."""
    # 응답이 리스트 형태일 경우 (FitCloud API 응답은 대부분 header/body 구조이므로 이 경우는 거의 없을 수 있습니다)
    if isinstance(response_data, list):
        return {"success": True, "data": response_data, "message": "조회 완료", "code": 200}
    
    # 응답이 딕셔너리 형태일 경우 (header/body 구조)
    if isinstance(response_data, dict):
        header = response_data.get('header', {})
        code = header.get('code')
        message = header.get('message', '')
        body = response_data.get('body', []) # 데이터가 없으면 빈 리스트

        # FitCloud API의 응답 코드에 따른 처리 로직
        if code == 200:
            return {"success": True, "data": body, "message": message, "code": code}
        elif code in [203, 204]: 
            # 데이터 없음, 그러나 성공적인 조회 응답으로 처리 (Bedrock Agent가 에러로 인식하지 않도록)
            print(f"ℹ️ FitCloud API 응답 코드 {code}: {message} (데이터 없음)")
            return {"success": True, "data": [], "message": message, "code": code} 
        else:
            # API 호출 자체는 성공했으나, FitCloud 내부 오류로 간주
            raise ValueError(f"FitCloud API 내부 오류 {code}: {message}")
    
    raise ValueError("FitCloud API에서 유효하지 않은 응답 형식을 받았습니다.")

def create_bedrock_response(event, status_code=200, response_data=None, error_message=None):
    """Bedrock Agent에 맞는 응답 형식을 생성합니다."""
    action_group = event.get('actionGroup', 'unknown')
    api_path_from_event = event.get('apiPath', '') 
    http_method = event.get('httpMethod', 'POST')
    
    if error_message:
        response_body = {
            "application/json": {
                # json.dumps에 custom_json_serializer를 default로 지정
                "body": json.dumps({
                    "error": error_message,
                    "success": False,
                    "timestamp": datetime.now(pytz.timezone('Asia/Seoul')), # datetime 객체 그대로 전달
                    "current_date_info": get_current_date_info() # datetime 객체 포함
                }, ensure_ascii=False, default=custom_json_serializer) 
            }
        }
        # 200으로 들어온 에러는 400으로 변경하여 Bedrock Agent가 에러로 처리하게 함 (Best Practice)
        status_code = 400 if status_code == 200 else status_code 
    else:
        response_body = {
            "application/json": {
                # json.dumps에 custom_json_serializer를 default로 지정
                "body": json.dumps(response_data, ensure_ascii=False, default=custom_json_serializer) 
            }
        }

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "apiPath": api_path_from_event,
            "httpMethod": http_method,
            "httpStatusCode": status_code,
            "responseBody": response_body 
        }
    }

def extract_parameters(event):
    """이벤트에서 파라미터를 추출합니다."""
    params = {}
    print(f"🔍 파라미터 추출 시작:")
    
    # Query Parameters (GET 요청 시)
    if 'parameters' in event and event['parameters']:
        print(f"  - Query Parameters 발견: {len(event['parameters'])}개")
        for param in event['parameters']:
            param_name = param.get('name')
            param_value = param.get('value')
            if param_name and param_value is not None:
                params[param_name] = param_value
                print(f"    * {param_name}: '{param_value}'")
    
    # Request Body Parameters (POST 요청 시)
    if 'requestBody' in event and 'content' in event['requestBody']:
        content = event['requestBody']['content']
        print(f"  - Request Body 발견")
        
        if 'application/x-www-form-urlencoded' in content:
            print(f"    * Content-Type: application/x-www-form-urlencoded")
            # 기존 body 필드 대신 properties 리스트에서 파라미터 추출
            properties = content['application/x-www-form-urlencoded'].get('properties')
            if properties:
                print(f"    * Properties 리스트에서 파라미터 추출 시작: {len(properties)}개 항목")
                for prop in properties:
                    name = prop.get('name')
                    value = prop.get('value')
                    if name and value is not None:
                        params[name] = value
                        print(f"      - {name}: '{value}'")
            else:
                # 기존 body 로직도 만약을 위해 유지 (다만 현재 케이스에서는 사용되지 않을 것)
                body_str = content['application/x-www-form-urlencoded'].get('body')
                if body_str:
                    print(f"    * Raw body_str for x-www-form-urlencoded (fallback): '{body_str}'") 
                    print(f"    * Body 내용 (fallback): '{body_str[:100]}...' (길이: {len(body_str)})")
                    parsed_body = parse_qs(body_str)
                    for key, value_list in parsed_body.items():
                        if value_list:
                            params[key] = value_list[0]
                            print(f"      - {key}: '{value_list[0]}'")
        
        elif 'application/json' in content:
            print(f"    * Content-Type: application/json")
            body_str = content['application/json'].get('body')
            if body_str:
                print(f"    * JSON Body 내용: '{body_str[:100]}...' (길이: {len(body_str)})")
                try:
                    json_body = json.loads(body_str)
                    params.update(json_body)
                    for key, value in json_body.items():
                        print(f"      - {key}: '{value}'")
                except json.JSONDecodeError as e:
                    print(f"    * ❌ JSON body 파싱 실패: {e}")
                    print(f"    * 원본 body: '{body_str}'")
    
    print(f"🔍 파라미터 추출 완료: {len(params)}개 파라미터")
    for key, value in params.items():
        print(f"  - {key}: '{value}' (타입: {type(value).__name__})")
    
    return params

def lambda_handler(event, context):
    print(f"--- API 호출 시작 (Bedrock Agent Event) ---")
    # 이벤트 로깅 시에도 custom_json_serializer 적용
    print(f"수신된 이벤트: {json.dumps(event, indent=2, default=custom_json_serializer)}") 

    session = create_retry_session()

    try:
        if 'messageVersion' not in event:
            return create_bedrock_response(event, 400, error_message="Invalid event format")

        action_group = event.get('actionGroup')
        api_path_from_event = event.get('apiPath')
        
        # 새로운 API 경로와 operationId 매핑 딕셔너리
        path_to_operation_map = {
            '/invoice/corp/monthly': 'getCorpMonthlyInvoice',
            '/invoice/account/monthly': 'getAccountMonthlyInvoice',
            '/usage/ondemand/monthly': 'getOndemandMonthlyUsage',
            '/usage/ondemand/daily': 'getOndemandDailyUsage',
            '/usage/ondemand/tags': 'getOndemandUsageByTags',
        }
        
        operation_id = path_to_operation_map.get(api_path_from_event)
        print(f"DEBUG: apiPath '{api_path_from_event}'로부터 operationId '{operation_id}' 유추")
        
        if not operation_id:
            return create_bedrock_response(event, 404, error_message=f"지원하지 않는 API 경로: {api_path_from_event}")

        params = extract_parameters(event)
        print(f"📝 추출된 파라미터: {params}")
        
        # 토큰 획득
        try:
            current_token = get_fitcloud_token()
            print("✅ 토큰 획득 성공")
        except RuntimeError as e:
            return create_bedrock_response(event, 401, error_message=f"인증 실패: {str(e)}")

        headers = {
            'Authorization': f'Bearer {current_token}',
            'User-Agent': 'FitCloud-Lambda/1.0'
        }

        response = None
        target_api_path = None

        # API 요청 로깅 함수
        def log_api_request(api_path, request_data, headers_info):
            """API 요청 정보를 CloudWatch에 로깅합니다."""
            print(f"🌐 FitCloud API 요청 정보:")
            print(f"  - URL: {FITCLOUD_BASE_URL}{api_path}")
            print(f"  - Method: POST")
            print(f"  - Headers: {json.dumps(headers_info, indent=2, default=custom_json_serializer)}")
            print(f"  - Request Data: {json.dumps(request_data, indent=2, ensure_ascii=False, default=custom_json_serializer)}")
            print(f"  - Content-Type: multipart/form-data")
            print(f"  - Timeout: 30초")

        # 공통 파라미터 체크 함수 (필수/선택 파라미터 추출)
        def check_and_get_params(required_params, optional_params=None):
            data = {}
            # 필수 파라미터 확인
            for p in required_params:
                if p not in params or params[p] is None:
                    raise ValueError(f"필수 파라미터 누락: '{p}'")
                data[p] = str(params[p]).strip() # 모든 파라미터를 문자열로 변환하고 공백 제거

            # 선택적 파라미터 추가
            if optional_params:
                for p in optional_params:
                    if p in params and params[p] is not None:
                        data[p] = str(params[p]).strip()
            return data

        # API 호출 로직 (operation_id 기반으로 분기)
        if operation_id == 'getCorpMonthlyInvoice':
            print("📞 법인 월별 청구서 조회 중...")
            target_api_path = '/invoice/corp/monthly'
            api_data = check_and_get_params(['billingPeriod'])
            
            billing_period = api_data['billingPeriod']
            if not (len(billing_period) == 6 and billing_period.isdigit()):
                raise ValueError(f"billingPeriod 형식이 올바르지 않습니다: {billing_period}. YYYYMM 형식(예: {get_current_date_info()['current_month_str']})을 사용해주세요.")
            
            # 미래 월 검증
            current_info = get_current_date_info()
            current_year = current_info['current_year']
            current_month = current_info['current_month']
            req_year = int(billing_period[:4])
            req_month = int(billing_period[4:])

            is_future_month = (req_year > current_year) or \
                              (req_year == current_year and req_month > current_month)
            if is_future_month:
                raise ValueError(f"요청하신 청구월({billing_period})이 미래입니다. 현재 월({current_year}{current_month:02d}) 이전의 월을 입력해주세요.")

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=30)
            
            raw_data = response.json()
            print("--- Raw API Response Start ---")
            print(json.dumps(raw_data, indent=2, ensure_ascii=False))
            print("--- Raw API Response End ---")
            processed_data_wrapper = process_fitcloud_response(raw_data)
            actual_items_data = processed_data_wrapper.get("data", [])
            
            final_response_content = {
                "success": processed_data_wrapper.get("success", True),
                "message": processed_data_wrapper.get("message", "조회가 완료되었습니다."),
                "timestamp": datetime.now(pytz.timezone('Asia/Seoul')), # datetime 객체 그대로 전달
                "billingPeriod": billing_period,
            }
            
            invoice_items = []
            total_usage_fee_krw = 0.0
            
            for item in actual_items_data:
                try:
                    processed_item = {
                        "billingPeriod": item.get("billingPeriod"),
                        "corpName": item.get("corpName"),
                        "invoiceDivision": item.get("invoiceDivision"),
                        "viewIndex": item.get("viewIndex"),
                        "lineItemType": item.get("lineItemType"),
                        "invoiceItem": item.get("invoiceItem"),
                        "currencyCode": item.get("currencyCode"),
                        "usageFee": float(item.get("usageFee", 0.0)),
                        "usageFeeKrw": float(item.get("usageFeeKrw", 0.0)),
                        "exchangeRate": item.get("exchangeRate"), # String 타입
                        "note": item.get("note"),
                    }
                    invoice_items.append(processed_item)
                    total_usage_fee_krw += float(item.get("usageFeeKrw", 0.0))
                except (ValueError, TypeError) as e:
                    print(f"경고: 청구서 항목 데이터 처리 오류 (항목 스킵): {item} - {e}")
                    continue
            
            final_response_content["invoice_items"] = invoice_items
            final_response_content["total_usage_fee_krw"] = round(total_usage_fee_krw, 2)
            final_response_content["item_count"] = len(invoice_items)

            if not invoice_items:
                final_response_content["message"] = f"{billing_period}에 대한 법인 월별 청구서 데이터가 없습니다."
                final_response_content["total_usage_fee_krw"] = 0.0

            return create_bedrock_response(event, 200, final_response_content)

        elif operation_id == 'getAccountMonthlyInvoice':
            print("📞 계정별 월별 청구서 조회 중...")
            target_api_path = '/invoice/account/monthly'
            api_data = check_and_get_params(['billingPeriod', 'accountId'])
            
            billing_period = api_data['billingPeriod']
            account_id = api_data['accountId']

            if not (len(billing_period) == 6 and billing_period.isdigit()):
                raise ValueError(f"billingPeriod 형식이 올바르지 않습니다: {billing_period}. YYYYMM 형식(예: {get_current_date_info()['current_month_str']})을 사용해주세요.")
            if not (len(account_id) == 12 and account_id.isdigit()):
                raise ValueError(f"accountId 형식이 올바르지 않습니다: {account_id}. 12자리 숫자를 입력해주세요.")

            # 미래 월 검증
            current_info = get_current_date_info()
            current_year = current_info['current_year']
            current_month = current_info['current_month']
            req_year = int(billing_period[:4])
            req_month = int(billing_period[4:])

            is_future_month = (req_year > current_year) or \
                              (req_year == current_year and req_month > current_month)
            if is_future_month:
                raise ValueError(f"요청하신 청구월({billing_period})이 미래입니다. 현재 월({current_year}{current_month:02d}) 이전의 월을 입력해주세요.")

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=30)
            
            raw_data = response.json()
            print("--- Raw API Response Start ---")
            print(json.dumps(raw_data, indent=2, ensure_ascii=False))
            print("--- Raw API Response End ---")
            processed_data_wrapper = process_fitcloud_response(raw_data)
            actual_items_data = processed_data_wrapper.get("data", [])
            
            final_response_content = {
                "success": processed_data_wrapper.get("success", True),
                "message": processed_data_wrapper.get("message", "조회가 완료되었습니다."),
                "timestamp": datetime.now(pytz.timezone('Asia/Seoul')), # datetime 객체 그대로 전달
                "billingPeriod": billing_period,
                "accountId": account_id,
            }
            
            invoice_items = []
            total_usage_fee_krw = 0.0
            
            for item in actual_items_data:
                try:
                    processed_item = {
                        "billingPeriod": item.get("billingPeriod"),
                        "corpName": item.get("corpName"),
                        "account_id": item.get("account_id"),
                        "invoiceDivision": item.get("invoiceDivision"),
                        "viewIndex": item.get("viewIndex"),
                        "lineItemType": item.get("lineItemType"),
                        "invoiceItem": item.get("invoiceItem"),
                        "currencyCode": item.get("currencyCode"),
                        "usageFee": float(item.get("usageFee", 0.0)),
                        "usageFeeKrw": float(item.get("usageFeeKrw", 0.0)),
                        "exchangeRate": item.get("exchangeRate"), # String 타입
                        "note": item.get("note"),
                    }
                    invoice_items.append(processed_item)
                    total_usage_fee_krw += float(item.get("usageFeeKrw", 0.0))
                except (ValueError, TypeError) as e:
                    print(f"경고: 청구서 항목 데이터 처리 오류 (항목 스킵): {item} - {e}")
                    continue
            
            final_response_content["invoice_items"] = invoice_items
            final_response_content["total_usage_fee_krw"] = round(total_usage_fee_krw, 2)
            final_response_content["item_count"] = len(invoice_items)

            if not invoice_items:
                final_response_content["message"] = f"{billing_period}에 대한 계정 {account_id}의 월별 청구서 데이터가 없습니다."
                final_response_content["total_usage_fee_krw"] = 0.0
            
            return create_bedrock_response(event, 200, final_response_content)

        elif operation_id == 'getOndemandMonthlyUsage':
            print("📞 월별 온디맨드 사용량 조회 중...")
            target_api_path = '/usage/ondemand/monthly'
            api_data = check_and_get_params(['from', 'to'])

            from_period = api_data['from']
            to_period = api_data['to']

            if not (len(from_period) == 6 and from_period.isdigit()):
                raise ValueError(f"시작 월 'from' 형식이 올바르지 않습니다: {from_period}. YYYYMM 형식(예: {get_current_date_info()['current_month_str']})을 사용해주세요.")
            if not (len(to_period) == 6 and to_period.isdigit()):
                raise ValueError(f"종료 월 'to' 형식이 올바르지 않습니다: {to_period}. YYYYMM 형식(예: {get_current_date_info()['current_month_str']})을 사용해주세요.")
            
            # 날짜 범위 유효성 검증
            try:
                from_date_obj_month = datetime.strptime(from_period, '%Y%m').date()
                to_date_obj_month = datetime.strptime(to_period, '%Y%m').date()
                current_month_str_yyyymm = get_current_date_info()['current_month_str']
                current_month_date_obj = datetime.strptime(current_month_str_yyyymm, '%Y%m').date()
                
                if from_date_obj_month > to_date_obj_month:
                    raise ValueError(f"조회 시작 월({from_period})이 종료 월({to_period})보다 늦습니다.")

                if from_date_obj_month > current_month_date_obj or to_date_obj_month > current_month_date_obj:
                    raise ValueError(f"요청하신 조회 기간({from_period}-{to_period})이 미래입니다. 현재 월({current_month_str_yyyymm}) 이전의 월을 입력해주세요.")

            except ValueError as e:
                raise ValueError(f"날짜 범위 오류: {e}. 유효한 YYYYMM 기간을 입력해주세요.")

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=60) 
            
            raw_data = response.json()
            print("--- Raw API Response Start ---")
            print(json.dumps(raw_data, indent=2, ensure_ascii=False))
            print("--- Raw API Response End ---")
            processed_data_wrapper = process_fitcloud_response(raw_data)
            actual_items_data = processed_data_wrapper.get("data", [])
            
            final_response_content = {
                "success": processed_data_wrapper.get("success", True),
                "message": processed_data_wrapper.get("message", "조회가 완료되었습니다."),
                "timestamp": datetime.now(pytz.timezone('Asia/Seoul')), # datetime 객체 그대로 전달
                "from": from_period,
                "to": to_period,
            }
            
            usage_items = []
            total_on_demand_cost = 0.0
            
            for item in actual_items_data:
                try:
                    # tagsJson이 문자열일 경우 JSON 객체로 파싱 시도
                    parsed_tags_json = {}
                    if 'tagsJson' in item and isinstance(item['tagsJson'], str):
                        try:
                            parsed_tags_json = json.loads(item['tagsJson'])
                        except json.JSONDecodeError:
                            print(f"경고: tagsJson 필드 파싱 실패 (유효하지 않은 JSON 문자열): {item['tagsJson']}")
                            parsed_tags_json = {}
                    elif 'tagsJson' in item and isinstance(item['tagsJson'], dict):
                        parsed_tags_json = item['tagsJson']

                    processed_item = {
                        "accountId": item.get("accountId"),
                        "usageType": item.get("usageType"),
                        "usageAmount": float(item.get("usageAmount", 0.0)),
                        "productCode": item.get("productCode"),
                        "region": item.get("region"),
                        "serviceCode": item.get("serviceCode"),
                        "tagsJson": parsed_tags_json,
                        "billingPeriod": item.get("billingPeriod"),
                        "onDemandCost": float(item.get("onDemandCost", 0.0)),
                        "billingEntity": item.get("billingEntity"),
                        "serviceName": item.get("serviceName"),
                    }
                    usage_items.append(processed_item)
                    total_on_demand_cost += float(item.get("onDemandCost", 0.0))
                except (ValueError, TypeError) as e:
                    print(f"경고: 온디맨드 사용량 항목 데이터 처리 오류 (항목 스킵): {item} - {e}")
                    continue
            
            final_response_content["usage_items"] = usage_items
            final_response_content["total_on_demand_cost"] = round(total_on_demand_cost, 2)
            final_response_content["item_count"] = len(usage_items)

            if not usage_items:
                final_response_content["message"] = f"{from_period}부터 {to_period}까지의 온디맨드 월별 사용량 데이터가 없습니다."
                final_response_content["total_on_demand_cost"] = 0.0
            
            return create_bedrock_response(event, 200, final_response_content)

        elif operation_id == 'getOndemandDailyUsage':
            print("📞 일별 온디맨드 사용량 조회 중...")
            target_api_path = '/usage/ondemand/daily'
            api_data = check_and_get_params(['from', 'to'])

            from_date_str = api_data['from']
            to_date_str = api_data['to']

            if not (len(from_date_str) == 8 and from_date_str.isdigit()):
                raise ValueError(f"시작일 'from' 형식이 올바르지 않습니다: {from_date_str}. YYYYMMDD 형식(예: {get_current_date_info()['current_date_str']})을 사용해주세요.")
            if not (len(to_date_str) == 8 and to_date_str.isdigit()):
                raise ValueError(f"종료일 'to' 형식이 올바르지 않습니다: {to_date_str}. YYYYMMDD 형식(예: {get_current_date_info()['current_date_str']})을 사용해주세요.")
            
            # 날짜 범위 유효성 검증
            try:
                from_date_obj = datetime.strptime(from_date_str, '%Y%m%d').date()
                to_date_obj = datetime.strptime(to_date_str, '%Y%m%d').date()
                current_date_only = get_current_date_info()['current_datetime'].date()
                
                if from_date_obj > to_date_obj:
                    raise ValueError(f"조회 시작일({from_date_str})이 종료일({to_date_str})보다 늦습니다.")

                if from_date_obj > current_date_only or to_date_obj > current_date_only:
                    raise ValueError(f"요청하신 조회 기간({from_date_str}-{to_date_str})이 미래입니다. 오늘({current_date_only.strftime('%Y%m%d')}) 이전의 날짜를 입력해주세요.")

            except ValueError as e:
                raise ValueError(f"날짜 범위 오류: {e}. 유효한 YYYYMMDD 기간을 입력해주세요.")

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=60) 
            
            raw_data = response.json()
            print("--- Raw API Response Start ---")
            print(json.dumps(raw_data, indent=2, ensure_ascii=False))
            print("--- Raw API Response End ---")
            processed_data_wrapper = process_fitcloud_response(raw_data)
            actual_items_data = processed_data_wrapper.get("data", [])
            
            final_response_content = {
                "success": processed_data_wrapper.get("success", True),
                "message": processed_data_wrapper.get("message", "조회가 완료되었습니다."),
                "timestamp": datetime.now(pytz.timezone('Asia/Seoul')), # datetime 객체 그대로 전달
                "from": from_date_str,
                "to": to_date_str,
            }
            
            usage_items = []
            total_on_demand_cost = 0.0
            
            for item in actual_items_data:
                try:
                    parsed_tags_json = {}
                    if 'tagsJson' in item and isinstance(item['tagsJson'], str):
                        try:
                            parsed_tags_json = json.loads(item['tagsJson'])
                        except json.JSONDecodeError:
                            print(f"경고: tagsJson 필드 파싱 실패 (유효하지 않은 JSON 문자열): {item['tagsJson']}")
                            parsed_tags_json = {}
                    elif 'tagsJson' in item and isinstance(item['tagsJson'], dict):
                        parsed_tags_json = item['tagsJson']

                    processed_item = {
                        "accountId": item.get("accountId"),
                        "usageType": item.get("usageType"),
                        "usageAmount": float(item.get("usageAmount", 0.0)),
                        "productCode": item.get("productCode"),
                        "region": item.get("region"),
                        "serviceCode": item.get("serviceCode"),
                        "tagsJson": parsed_tags_json,
                        "billingPeriod": item.get("billingPeriod"), # YYYYMMDD 형식 예상
                        "onDemandCost": float(item.get("onDemandCost", 0.0)),
                        "billingEntity": item.get("billingEntity"),
                        "serviceName": item.get("serviceName"),
                    }
                    usage_items.append(processed_item)
                    total_on_demand_cost += float(item.get("onDemandCost", 0.0))
                except (ValueError, TypeError) as e:
                    print(f"경고: 온디맨드 사용량 항목 데이터 처리 오류 (항목 스킵): {item} - {e}")
                    continue
            
            final_response_content["usage_items"] = usage_items
            final_response_content["total_on_demand_cost"] = round(total_on_demand_cost, 2)
            final_response_content["item_count"] = len(usage_items)

            if not usage_items:
                final_response_content["message"] = f"{from_date_str}부터 {to_date_str}까지의 온디맨드 일별 사용량 데이터가 없습니다."
                final_response_content["total_on_demand_cost"] = 0.0
            
            return create_bedrock_response(event, 200, final_response_content)

        elif operation_id == 'getOndemandUsageByTags':
            print("📞 태그별 온디맨드 사용량 상세 조회 중...")
            target_api_path = '/usage/ondemand/tags'
            api_data = check_and_get_params(['beginDate', 'endDate'])

            begin_date_str = api_data['beginDate']
            end_date_str = api_data['endDate']

            if not (len(begin_date_str) == 8 and begin_date_str.isdigit()):
                raise ValueError(f"시작일 'beginDate' 형식이 올바르지 않습니다: {begin_date_str}. YYYYMMDD 형식(예: {get_current_date_info()['current_date_str']})을 사용해주세요.")
            if not (len(end_date_str) == 8 and end_date_str.isdigit()):
                raise ValueError(f"종료일 'endDate' 형식이 올바르지 않습니다: {end_date_str}. YYYYMMDD 형식(예: {get_current_date_info()['current_date_str']})을 사용해주세요.")
            
            # 날짜 범위 유효성 검증 (일별 온디맨드 사용량과 동일)
            try:
                begin_date_obj = datetime.strptime(begin_date_str, '%Y%m%d').date()
                end_date_obj = datetime.strptime(end_date_str, '%Y%m%d').date()
                current_date_only = get_current_date_info()['current_datetime'].date() 
                
                if begin_date_obj > end_date_obj:
                    raise ValueError(f"조회 시작일({begin_date_str})이 종료일({end_date_str})보다 늦습니다.")

                if begin_date_obj > current_date_only or end_date_obj > current_date_only:
                    raise ValueError(f"요청하신 조회 기간({begin_date_str}-{end_date_str})이 미래입니다. 오늘({current_date_only.strftime('%Y%m%d')}) 이전의 날짜를 입력해주세요.")

            except ValueError as e:
                raise ValueError(f"날짜 범위 오류: {e}. 유효한 YYYYMMDD 기간을 입력해주세요.")

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=60) 
            
            raw_data = response.json()
            print("--- Raw API Response Start ---")
            print(json.dumps(raw_data, indent=2, ensure_ascii=False))
            print("--- Raw API Response End ---")
            processed_data_wrapper = process_fitcloud_response(raw_data)
            actual_items_data = processed_data_wrapper.get("data", [])
            
            final_response_content = {
                "success": processed_data_wrapper.get("success", True),
                "message": processed_data_wrapper.get("message", "조회가 완료되었습니다."),
                "timestamp": datetime.now(pytz.timezone('Asia/Seoul')), # datetime 객체 그대로 전달
                "beginDate": begin_date_str,
                "endDate": end_date_str,
            }
            
            usage_tag_items = []
            total_on_demand_cost = 0.0
            
            for item in actual_items_data:
                try:
                    parsed_tags_json = {}
                    if 'tagsJson' in item and isinstance(item['tagsJson'], str):
                        try:
                            parsed_tags_json = json.loads(item['tagsJson'])
                        except json.JSONDecodeError:
                            print(f"경고: tagsJson 필드 파싱 실패 (유효하지 않은 JSON 문자열): {item['tagsJson']}")
                            parsed_tags_json = {}
                    elif 'tagsJson' in item and isinstance(item['tagsJson'], dict):
                        parsed_tags_json = item['tagsJson']

                    # usageAmount와 onDemandCost를 float으로 변환 시도 (String으로 올 경우 대비)
                    usage_amount = float(item.get("usageAmount", 0.0)) if item.get("usageAmount") else 0.0
                    on_demand_cost = float(item.get("onDemandCost", 0.0)) if item.get("onDemandCost") else 0.0

                    processed_item = {
                        "serviceCode": item.get("serviceCode"),
                        "usageAmount": usage_amount,
                        "resourceTags": item.get("resourceTags"),
                        "accountId": item.get("accountId"),
                        "unit": item.get("unit"),
                        "onDemandCost": on_demand_cost,
                        "region": item.get("region"),
                        "operation": item.get("operation"),
                        "tagsJson": parsed_tags_json, 
                        "usageType": item.get("usageType"),
                        "billingEntity": item.get("billingEntity"),
                        "serviceName": item.get("serviceName"),
                    }
                    usage_tag_items.append(processed_item)
                    total_on_demand_cost += on_demand_cost
                except (ValueError, TypeError) as e:
                    print(f"경고: 온디맨드 태그별 사용량 항목 데이터 처리 오류 (항목 스킵): {item} - {e}")
                    continue
            
            final_response_content["usage_tag_items"] = usage_tag_items
            final_response_content["total_on_demand_cost"] = round(total_on_demand_cost, 2)
            final_response_content["item_count"] = len(usage_tag_items)

            if not usage_tag_items:
                final_response_content["message"] = f"{begin_date_str}부터 {end_date_str}까지의 태그별 온디맨드 사용량 데이터가 없습니다."
                final_response_content["total_on_demand_cost"] = 0.0
            
            return create_bedrock_response(event, 200, final_response_content)
        
        else:
            return create_bedrock_response(event, 400, error_message=f"알 수 없는 operationId: {operation_id}")

    except ValueError as ve:
        print(f"❌ 입력 파라미터 또는 비즈니스 로직 오류: {ve}")
        return create_bedrock_response(event, 400, error_message=str(ve))
    except requests.exceptions.RequestException as re:
        print(f"❌ HTTP 요청 오류: {re}")
        return create_bedrock_response(event, 503, error_message=f"외부 API 호출 중 네트워크 오류가 발생했습니다. 잠시 후 다시 시도해주세요. 오류: {str(re)}")
    except json.JSONDecodeError as jde:
        print(f"❌ JSON 파싱 오류: {jde}")
        return create_bedrock_response(event, 500, error_message=f"외부 API 응답을 처리하는 중 JSON 파싱 오류가 발생했습니다. 오류: {str(jde)}")
    except Exception as e:
        print(f"❌ 예기치 않은 오류 발생: {e}", exc_info=True)
        return create_bedrock_response(event, 500, error_message=f"서비스 내부 오류가 발생했습니다. 잠시 후 다시 시도해주세요. 오류: {str(e)}")