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
    utc_now = datetime.utcnow()
    tz = pytz.timezone('Asia/Seoul')
    utc_with_tz = pytz.utc.localize(utc_now)
    now = utc_with_tz.astimezone(tz)
    
    current_info = {
        'current_year': now.year,
        'current_month': now.month,
        'current_day': now.day,
        'current_datetime': now,
        'current_date_str': now.strftime('%Y%m%d'),
        'current_month_str': now.strftime('%Y%m'),
        'timezone': str(now.tzinfo),
        'utc_time': utc_now.isoformat(),
        'kst_time': now.isoformat()
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
    
    # 현재 날짜 정보를 sessionAttributes에 포함
    current_date_info = get_current_date_info()
    session_attributes = {
        'current_year': str(current_date_info['current_year']),
        'current_month': str(current_date_info['current_month']),
        'current_day': str(current_date_info['current_day']),
        'current_date': current_date_info['current_date_str'],
        'current_month_str': current_date_info['current_month_str']
    }
    
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
        },
        "sessionAttributes": session_attributes
    }

def extract_parameters(event):
    """이벤트에서 파라미터를 추출합니다."""
    params = {}
    session_current_year = None
    available_accounts = []
    
    # 세션 속성에서 날짜 정보와 계정 정보 가져오기
    if 'sessionAttributes' in event:
        session_attrs = event['sessionAttributes']
        if 'current_year' in session_attrs:
            session_current_year = str(session_attrs['current_year'])
        
        # 계정 정보 가져오기
        if 'available_accounts' in session_attrs:
            try:
                available_accounts = json.loads(session_attrs['available_accounts'])
            except json.JSONDecodeError:
                available_accounts = []
    
    # Query Parameters (GET 요청 시)
    if 'parameters' in event and event['parameters']:
        for param in event['parameters']:
            param_name = param.get('name')
            param_value = param.get('value')
            if param_name and param_value is not None:
                params[param_name] = param_value
    
    # Request Body Parameters (POST 요청 시)
    if 'requestBody' in event and 'content' in event['requestBody']:
        content = event['requestBody']['content']
        
        if 'application/x-www-form-urlencoded' in content:
            properties = content['application/x-www-form-urlencoded'].get('properties')
            if properties:
                for prop in properties:
                    name = prop.get('name')
                    value = prop.get('value')
                    if name and value is not None:
                        params[name] = value
            else:
                body_str = content['application/x-www-form-urlencoded'].get('body')
                if body_str:
                    parsed_body = parse_qs(body_str)
                    for key, value_list in parsed_body.items():
                        if value_list:
                            params[key] = value_list[0]
        
        elif 'application/json' in content:
            body_str = content['application/json'].get('body')
            if body_str:
                try:
                    json_body = json.loads(body_str)
                    params.update(json_body)
                except json.JSONDecodeError:
                    pass
    
    # 월만 입력된 경우 보정
    for k, v in list(params.items()):
        if k in ['from', 'to', 'billingPeriod', 'beginDate', 'endDate']:
            v_str = str(v)
            # MMDD 형태(4자리)일 때 연도 보정
            if len(v_str) == 4 and v_str.isdigit() and session_current_year:
                try:
                    test_date_str = f"{session_current_year}{v_str}"
                    datetime.strptime(test_date_str, '%Y%m%d')
                    params[k] = test_date_str
                    print(f"📅 MMDD 보정: {k}={v} → {params[k]}")
                    continue
                except ValueError:
                    pass
            # 월만 입력된 경우 (1~2자리)
            if (len(v_str) == 1 or (len(v_str) == 2 and v_str.isdigit())) and session_current_year:
                params[k] = f"{session_current_year}{v_str.zfill(2)}"
                print(f"📅 월 보정: {k}={v} → {params[k]}")
    
    # 계정명으로 계정ID 찾기
    if available_accounts and 'accountName' in params and not 'accountId' in params:
        account_name = str(params['accountName']).strip()
        
        for account in available_accounts:
            if account.get('accountName', '').strip().lower() == account_name.lower():
                found_account_id = account.get('accountId')
                params['accountId'] = found_account_id
                print(f"🔍 계정명 매칭: {account_name} → {found_account_id}")
                break
    
    return params

# 안전한 float 변환 함수 추가
def validate_date_logic(params, api_path=None):
    """
    API 경로별로 필수 파라미터와 날짜 형식을 검증합니다.
    """
    warnings = []
    current_info = get_current_date_info()
    current_date_only = current_info['current_datetime'].date()
    
    print(f"🔍 날짜 검증: {api_path}")
    
    # API별 필수 파라미터 정의
    api_requirements = {
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
                warnings.append(f"'{param}' 파라미터는 YYYYMM 형식이어야 합니다: {param_value}")
            elif expected_format == 'YYYYMMDD' and not (len(param_value) == 8 and param_value.isdigit()):
                warnings.append(f"'{param}' 파라미터는 YYYYMMDD 형식이어야 합니다: {param_value}")
    
    # billingPeriod 검증 (청구서 API용)
    if 'billingPeriod' in params:
        billing_period = str(params['billingPeriod'])
        if len(billing_period) == 6:
            try:
                year = int(billing_period[:4])
                month = int(billing_period[4:])
                current_year = current_info['current_year']
                current_month = current_info['current_month']
                
                is_future_month = (year > current_year) or \
                                (year == current_year and month > current_month)
                
                if is_future_month:
                    warnings.append(f"요청하신 월이 미래입니다: {billing_period}")
                    
            except ValueError as e:
                warnings.append(f"billingPeriod 파싱 오류: {e}")
    
    # from/to 파라미터 검증
    if 'from' in params and 'to' in params:
        from_str = str(params['from'])
        to_str = str(params['to'])
        
        try:
            is_daily_format = False
            from_dt_obj = None
            to_dt_obj = None

            if len(from_str) == 8 and len(to_str) == 8:
                from_dt_obj = datetime.strptime(from_str, '%Y%m%d').date()
                to_dt_obj = datetime.strptime(to_str, '%Y%m%d').date()
                is_daily_format = True
            elif len(from_str) == 6 and len(to_str) == 6:
                from_dt_obj = datetime.strptime(from_str + '01', '%Y%m%d').date()
                next_month = (datetime.strptime(to_str + '01', '%Y%m%d').replace(day=1) + timedelta(days=32)).replace(day=1)
                to_dt_obj = (next_month - timedelta(days=1)).date()
            else:
                warnings.append("날짜 형식이 올바르지 않습니다 (YYYYMM 또는 YYYYMMDD).")
                return warnings
            
            if from_dt_obj > to_dt_obj:
                warnings.append("조회 시작일이 종료일보다 늦습니다.")

            if is_daily_format:
                if from_dt_obj > current_date_only or to_dt_obj > current_date_only:
                    warnings.append(f"요청하신 날짜가 미래입니다: {from_str} - {to_str}")
            else:
                req_from_year = int(from_str[:4])
                req_from_month = int(from_str[4:])
                req_to_year = int(to_str[:4])
                req_to_month = int(to_str[4:])
                
                current_year = current_info['current_year']
                current_month = current_info['current_month']
                
                is_from_future_month = (req_from_year > current_year) or \
                                     (req_from_year == current_year and req_from_month > current_month)
                is_to_future_month = (req_to_year > current_year) or \
                                   (req_to_year == current_year and req_to_month > current_month)
                
                if is_from_future_month or is_to_future_month:
                    warnings.append(f"요청하신 월이 미래입니다: {from_str} - {to_str}")
                    
        except ValueError as e:
            warnings.append(f"날짜 파싱 오류: {e}")
    
    # beginDate/endDate 파라미터 검증
    if 'beginDate' in params and 'endDate' in params:
        begin_str = str(params['beginDate'])
        end_str = str(params['endDate'])
        
        try:
            if len(begin_str) == 8 and len(end_str) == 8:
                begin_dt_obj = datetime.strptime(begin_str, '%Y%m%d').date()
                end_dt_obj = datetime.strptime(end_str, '%Y%m%d').date()
                
                if begin_dt_obj > end_dt_obj:
                    warnings.append("조회 시작일이 종료일보다 늦습니다.")

                if begin_dt_obj > current_date_only or end_dt_obj > current_date_only:
                    warnings.append(f"요청하신 날짜가 미래입니다: {begin_str} - {end_str}")
            else:
                warnings.append("날짜 형식이 올바르지 않습니다 (YYYYMMDD).")
                return warnings
                    
        except ValueError as e:
            warnings.append(f"날짜 파싱 오류: {e}")

    return warnings

def safe_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0

def lambda_handler(event, context):
    print(f"🚀 Lambda 2 시작: {event.get('apiPath', 'N/A')}")

    session = create_retry_session()

    try:
        if 'messageVersion' not in event:
            return create_bedrock_response(event, 400, error_message="Invalid event format")

        action_group = event.get('actionGroup')
        api_path_from_event = event.get('apiPath')
        
        # API 경로와 operationId 매핑
        path_to_operation_map = {
            '/invoice/corp/monthly': 'getCorpMonthlyInvoice',
            '/invoice/account/monthly': 'getAccountMonthlyInvoice',
            '/usage/ondemand/monthly': 'getOndemandMonthlyUsage',
            '/usage/ondemand/daily': 'getOndemandDailyUsage',
            '/usage/ondemand/tags': 'getOndemandUsageByTags',
        }
        
        operation_id = path_to_operation_map.get(api_path_from_event)
        
        if not operation_id:
            return create_bedrock_response(event, 404, error_message=f"지원하지 않는 API 경로: {api_path_from_event}")

        params = extract_parameters(event)
        print(f"📝 파라미터: {params}")
        
        # billingPeriod를 from/to로 변환
        if 'billingPeriod' in params and not ('from' in params and 'to' in params):
            billing_period = str(params['billingPeriod'])
            if len(billing_period) == 6:
                params['from'] = billing_period
                params['to'] = billing_period
                print(f"🔄 billingPeriod 변환: {billing_period} → from/to")
        
        # ✨ 날짜 검증 로직 적용 ✨
        date_warnings = validate_date_logic(params, api_path_from_event)
        if date_warnings:
            print(f"DEBUG: 날짜 유효성 검증 경고: {date_warnings}")
            # 400 Bad Request로 응답하여 Agent가 재요청하거나 사용자에게 알리도록 함
            return create_bedrock_response(
                event, 400, 
                error_message=f"날짜 오류: {'; '.join(date_warnings)}. 유효한 날짜 또는 기간을 입력해주세요."
            )
        print(f"📝 최종 확인 파라미터: {params}")
        # ✨ 날짜 검증 로직 적용 끝 ✨
        
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
            """API 요청 정보를 로깅합니다."""
            print(f"🌐 API 호출: {api_path}")
            print(f"  - 파라미터: {request_data}")

        # 공통 파라미터 체크 함수 (필수/선택 파라미터 추출)
        def check_and_get_params(required_params, optional_params=None):
            data = {}
            # 필수 파라미터 확인
            for p in required_params:
                val = params.get(p)
                if val is None:
                    raise ValueError(f"필수 파라미터 누락: '{p}'")
                # extract_parameters에서 이미 날짜 보정이 완료되었으므로 그대로 사용
                data[p] = str(val).strip()
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
            prepared_data = prepare_form_data(api_data)
            log_api_request(target_api_path, api_data, headers)
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=30)
            raw_data = response.json()
            print(f"✅ API 응답 수신: {len(raw_data.get('body', []))}개 항목")
            processed_data_wrapper = process_fitcloud_response(raw_data)
            actual_items_data = processed_data_wrapper.get("data", [])
            # USD 기준 합산 및 표기
            invoice_items = []
            total_invoice_fee_usd = 0.0
            for item in actual_items_data:
                try:
                    fee_usd = safe_float(item.get("usageFee", 0.0))
                    invoice_items.append({
                        "serviceName": item.get("invoiceItem", item.get("serviceName", "알 수 없음")),
                        "usageFeeUSD": round(fee_usd, 2),
                        "currencyCode": item.get("currencyCode", "USD"),
                        "note": item.get("note", ""),
                        "lineItemType": item.get("lineItemType", ""),
                        "viewIndex": item.get("viewIndex", "")
                    })
                    total_invoice_fee_usd += fee_usd
                except Exception as e:
                    print(f"[invoice_items] USD 합산 오류: {e}")
                    continue
            final_response_content = {
                "success": processed_data_wrapper.get("success", True),
                "message": processed_data_wrapper.get("message", "조회가 완료되었습니다."),
                "timestamp": datetime.now(pytz.timezone('Asia/Seoul')),
                "billingPeriod": billing_period,
                "invoice_items": invoice_items,
                "total_invoice_fee_usd": round(total_invoice_fee_usd, 2),
                "item_count": len(invoice_items)
            }
            if not invoice_items:
                final_response_content["message"] = f"{billing_period}에 대한 법인 월별 청구서 데이터가 없습니다."
                final_response_content["total_invoice_fee_usd"] = 0.0
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
            prepared_data = prepare_form_data(api_data)
            log_api_request(target_api_path, api_data, headers)
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=30)
            raw_data = response.json()
            print(f"✅ API 응답 수신: {len(raw_data.get('body', []))}개 항목")
            processed_data_wrapper = process_fitcloud_response(raw_data)
            actual_items_data = processed_data_wrapper.get("data", [])
            # accountId로 필터링 추가
            filtered_items_data = [item for item in actual_items_data if str(item.get("accountId")) == str(account_id)]
            # USD 기준 합산 및 표기
            invoice_items = []
            total_invoice_fee_usd = 0.0
            for item in filtered_items_data:
                try:
                    fee_usd = safe_float(item.get("usageFee", 0.0))
                    invoice_items.append({
                        "serviceName": item.get("invoiceItem", item.get("serviceName", "알 수 없음")),
                        "usageFeeUSD": round(fee_usd, 2),
                        "currencyCode": item.get("currencyCode", "USD"),
                        "note": item.get("note", ""),
                        "lineItemType": item.get("lineItemType", ""),
                        "viewIndex": item.get("viewIndex", "")
                    })
                    total_invoice_fee_usd += fee_usd
                except Exception as e:
                    print(f"[invoice_items] USD 합산 오류: {e}")
                    continue
            final_response_content = {
                "success": processed_data_wrapper.get("success", True),
                "message": processed_data_wrapper.get("message", "조회가 완료되었습니다."),
                "timestamp": datetime.now(pytz.timezone('Asia/Seoul')),
                "billingPeriod": billing_period,
                "accountId": account_id,
                "invoice_items": invoice_items,
                "total_invoice_fee_usd": round(total_invoice_fee_usd, 2),
                "item_count": len(invoice_items)
            }
            if not invoice_items:
                final_response_content["message"] = f"{billing_period}에 대한 계정 {account_id}의 월별 청구서 데이터가 없습니다."
                final_response_content["total_invoice_fee_usd"] = 0.0
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

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=60) 
            
            raw_data = response.json()
            print(f"✅ API 응답 수신: {len(raw_data.get('body', []))}개 항목")
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
                        "usageAmount": safe_float(item.get("usageAmount", 0.0)),
                        "productCode": item.get("productCode"),
                        "region": item.get("region"),
                        "serviceCode": item.get("serviceCode"),
                        "tagsJson": parsed_tags_json,
                        "billingPeriod": item.get("billingPeriod"),
                        "onDemandCost": safe_float(item.get("onDemandCost", 0.0)),
                        "billingEntity": item.get("billingEntity"),
                        "serviceName": item.get("serviceName"),
                    }
                    usage_items.append(processed_item)
                    total_on_demand_cost += safe_float(item.get("onDemandCost", 0.0))
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

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=60) 
            
            raw_data = response.json()
            print(f"✅ API 응답 수신: {len(raw_data.get('body', []))}개 항목")
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
                        "usageAmount": safe_float(item.get("usageAmount", 0.0)),
                        "productCode": item.get("productCode"),
                        "region": item.get("region"),
                        "serviceCode": item.get("serviceCode"),
                        "tagsJson": parsed_tags_json,
                        "billingPeriod": item.get("billingPeriod"), # YYYYMMDD 형식 예상
                        "onDemandCost": safe_float(item.get("onDemandCost", 0.0)),
                        "billingEntity": item.get("billingEntity"),
                        "serviceName": item.get("serviceName"),
                    }
                    usage_items.append(processed_item)
                    total_on_demand_cost += safe_float(item.get("onDemandCost", 0.0))
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

            prepared_data = prepare_form_data(api_data)
            
            # API 요청 로깅
            log_api_request(target_api_path, api_data, headers)
            
            response = session.post(f'{FITCLOUD_BASE_URL}{target_api_path}', headers=headers, files=prepared_data, timeout=60) 
            
            raw_data = response.json()
            print(f"✅ API 응답 수신: {len(raw_data.get('body', []))}개 항목")
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
                    usage_amount = safe_float(item.get("usageAmount", 0.0))
                    on_demand_cost = safe_float(item.get("onDemandCost", 0.0))

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