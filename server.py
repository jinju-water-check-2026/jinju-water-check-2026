from flask import Flask, jsonify, request, send_from_directory
from urllib.parse import unquote
import os, math, requests, xml.etree.ElementTree as ET
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = 'index.html'
app = Flask(__name__, static_folder=None)

# 공공데이터포털(data.go.kr)에서 발급받은 일반 인증키(Decoding)를 환경변수에 넣으세요.
def load_service_key():
    # 1) 환경변수가 있으면 우선 사용
    env_key = os.environ.get('KMA_SERVICE_KEY', '').strip()
    if env_key:
        return env_key

    # 2) 배포용 폴더의 config/api_key.txt에서 읽기
    key_file = os.path.join(BASE_DIR, 'config', 'api_key.txt')
    try:
        with open(key_file, 'r', encoding='utf-8-sig') as f:
            key = f.read().strip()
    except FileNotFoundError:
        return ''

    if not key or key == 'PASTE_YOUR_DATA_GO_KR_API_KEY_HERE':
        return ''
    return key

SERVICE_KEY = load_service_key()

# 지역별 대표 방재기상관측(AWS)을 사용합니다.
# 금남면·진교면은 금남 AWS(933), 곤양면은 사천 AWS(917)을 대표 관측지점으로 지정합니다.
# AWS의 과거 일자료는 지역별 강수량/무강우 분석에 사용하고, 오늘 실황·미래 예보는 해당 관측지점 좌표의 대표 격자로 조회합니다.
REGIONS = {
    'geumnam': {'name': '하동군 금남면', 'stn': '933', 'station_name': '금남 AWS(933)', 'lat': 34.95056, 'lon': 127.85902},
    'jingyo':  {'name': '하동군 진교면', 'stn': '933', 'station_name': '금남 AWS(933)', 'lat': 34.95056, 'lon': 127.85902},
    'gonyang': {'name': '사천시 곤양면', 'stn': '917', 'station_name': '사천 AWS(917)', 'lat': 35.03695, 'lon': 128.06768},
}

AWS_DAILY_URL = 'http://apis.data.go.kr/1360000/AwsDalyInfoService/getWthrDataList'
ASOS_URL = 'http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList'

FCST_URL = 'https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst'
NCST_URL = 'https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst'


def require_key():
    if not SERVICE_KEY:
        raise RuntimeError('공공데이터포털 인증키가 설정되지 않았습니다. config/api_key.txt에 인증키를 저장해 주세요.')


def normalize_key(key: str) -> str:
    # Encoding 키를 붙여넣어도 requests가 재인코딩하지 않도록 decoding 처리
    return unquote(key)


def parse_kma_response(r, label='KMA API'):
    """JSON 정상응답과 XML/HTML 오류응답을 모두 읽어 사람이 이해할 메시지로 바꿉니다."""
    text = r.text or ''
    if not r.ok:
        raise RuntimeError(f'{label} HTTP {r.status_code}: {text[:300]}')
    try:
        return r.json()
    except Exception:
        # 공공데이터포털 인증/권한 오류는 XML로 오는 경우가 많음
        msg = ''
        code = ''
        try:
            root = ET.fromstring(text)
            for tag in ('returnAuthMsg','resultMsg','errMsg','message'):
                el = root.find('.//' + tag)
                if el is not None and el.text:
                    msg = el.text.strip(); break
            for tag in ('returnReasonCode','resultCode','errCode'):
                el = root.find('.//' + tag)
                if el is not None and el.text:
                    code = el.text.strip(); break
        except Exception:
            pass
        detail = ' / '.join(x for x in (code, msg) if x) or text[:300] or '응답 본문 없음'
        raise RuntimeError(f'{label} 응답 오류: {detail}')

def aws_daily(start, end, stn):
    require_key()
    params = {
        'ServiceKey': normalize_key(SERVICE_KEY),
        'pageNo': '1', 'numOfRows': '999', 'dataType': 'XML',
        'dataCd': 'AWS', 'dateCd': 'DAY',
        'startDt': start.replace('-', ''), 'endDt': end.replace('-', ''),
        'stnIds': stn,
    }
    r = requests.get(AWS_DAILY_URL, params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f'AWS 일자료 API HTTP {r.status_code}')
    text = r.text or ''
    try:
        root = ET.fromstring(text)
    except Exception as e:
        raise RuntimeError('AWS 일자료 XML 응답을 읽지 못했습니다.') from e
    code = (root.findtext('.//resultCode') or root.findtext('.//returnReasonCode') or '').strip()
    msg = (root.findtext('.//resultMsg') or root.findtext('.//returnAuthMsg') or '').strip()
    if code not in ('00', '0'):
        raise RuntimeError(f'AWS API 오류 {code}: {msg or "알 수 없는 오류"}')
    items = []
    for item in root.findall('.//item'):
        row = {}
        for child in list(item):
            row[child.tag] = child.text or ''
        items.append(row)
    return items


def asos_daily(start, end, stn='192'):
    require_key()
    # 공공데이터포털 ASOS 공식 명세와 동일하게 HTTP + ServiceKey + XML 사용
    # (브라우저의 OpenAPI 테스트와 같은 호출 방식)
    params = {
        'ServiceKey': normalize_key(SERVICE_KEY),
        'pageNo': '1', 'numOfRows': '999', 'dataType': 'XML',
        'dataCd': 'ASOS', 'dateCd': 'DAY',
        'startDt': start.replace('-', ''), 'endDt': end.replace('-', ''),
        'stnIds': stn,
    }
    r = requests.get(ASOS_URL, params=params, timeout=30)
    if not r.ok:
        # 인증키가 포함된 전체 URL은 로그에 남기지 않음
        raise RuntimeError(f'ASOS 일자료 API HTTP {r.status_code}')

    text = r.text or ''
    try:
        root = ET.fromstring(text)
    except Exception as e:
        raise RuntimeError('ASOS 일자료 XML 응답을 읽지 못했습니다.') from e

    code = (root.findtext('.//resultCode') or root.findtext('.//returnReasonCode') or '').strip()
    msg = (root.findtext('.//resultMsg') or root.findtext('.//returnAuthMsg') or '').strip()
    if code not in ('00', '0'):
        raise RuntimeError(f'ASOS API 오류 {code}: {msg or "알 수 없는 오류"}')

    items = []
    for item in root.findall('.//item'):
        row = {}
        for child in list(item):
            row[child.tag] = child.text or ''
        items.append(row)
    return items


def dry_analysis(items, threshold=0.1):
    rows = []
    for x in items:
        date = str(x.get('tm', ''))[:10]
        raw = x.get('sumRn')
        try:
            rain = float(raw) if raw not in ('', None) else 0.0
        except Exception:
            rain = 0.0
        if date:
            rows.append((date, rain))
    rows.sort()
    runs, cur_start, cur_end, cur_days = [], None, None, 0
    for date, rain in rows:
        if rain < threshold:
            if cur_start is None:
                cur_start = date
            cur_end = date
            cur_days += 1
        else:
            if cur_days:
                runs.append({'start': cur_start, 'end': cur_end, 'days': cur_days})
            cur_start = cur_end = None
            cur_days = 0
    if cur_days:
        runs.append({'start': cur_start, 'end': cur_end, 'days': cur_days})
    max_run = max(runs, key=lambda z: z['days'], default=None)
    qualifying = [z for z in runs if z['days'] >= 21]
    latest = rows[-1][1] if rows else None
    current_run = runs[-1] if runs and rows and runs[-1].get('end') == rows[-1][0] else None
    return latest, max_run, qualifying, current_run


def dfs_xy(lat, lon):
    # 기상청 단기예보 격자 변환 공식 (Lambert Conformal Conic)
    RE, GRID, SLAT1, SLAT2, OLON, OLAT, XO, YO = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43.0, 136.0
    DEGRAD = math.pi / 180.0
    re = RE / GRID
    slat1, slat2, olon, olat = SLAT1*DEGRAD, SLAT2*DEGRAD, OLON*DEGRAD, OLAT*DEGRAD
    sn = math.tan(math.pi*0.25 + slat2*0.5) / math.tan(math.pi*0.25 + slat1*0.5)
    sn = math.log(math.cos(slat1)/math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi*0.25 + slat1*0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi*0.25 + olat*0.5)
    ro = re * sf / math.pow(ro, sn)
    ra = math.tan(math.pi*0.25 + lat*DEGRAD*0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon*DEGRAD - olon
    if theta > math.pi: theta -= 2.0*math.pi
    if theta < -math.pi: theta += 2.0*math.pi
    theta *= sn
    return int(ra*math.sin(theta) + XO + 0.5), int(ro - ra*math.cos(theta) + YO + 0.5)



def latest_ncst_base(now=None):
    """초단기실황은 매시각 자료가 생성되므로 API 반영 지연을 고려해 약 40분 전의 정시 자료를 사용합니다."""
    t = (now or datetime.now()) - timedelta(minutes=40)
    return t.strftime('%Y%m%d'), t.strftime('%H00')


def ultra_nowcast(region_key):
    """단기예보 조회서비스의 초단기실황(getUltraSrtNcst)으로 현재에 가까운 AWS 대표 관측값을 조회합니다."""
    require_key()
    info = REGIONS.get(region_key)
    if not info:
        raise RuntimeError('지원하지 않는 지역입니다.')
    nx, ny = dfs_xy(info['lat'], info['lon'])
    base_date, base_time = latest_ncst_base()
    params = {
        'serviceKey': normalize_key(SERVICE_KEY),
        'pageNo': '1', 'numOfRows': '1000', 'dataType': 'JSON',
        'base_date': base_date, 'base_time': base_time,
        'nx': str(nx), 'ny': str(ny),
    }
    r = requests.get(NCST_URL, params=params, timeout=20)
    data = parse_kma_response(r, '초단기실황 API')
    header = data.get('response', {}).get('header', {})
    if str(header.get('resultCode', '')) not in ('00', '0'):
        raise RuntimeError(header.get('resultMsg', '초단기실황 API 오류'))
    raw = data.get('response', {}).get('body', {}).get('items', {}).get('item', []) or []
    vals = {x.get('category'): x.get('obsrValue') for x in raw}
    return {
        'regionKey': region_key,
        'region': info['name'],
        'grid': {'nx': nx, 'ny': ny},
        'baseDate': base_date,
        'baseTime': base_time,
        'rain1h': vals.get('RN1'),
        'pty': vals.get('PTY'),
        'temp': vals.get('T1H'),
        'humidity': vals.get('REH'),
        'wind': vals.get('WSD'),
        'status': 'ok',
    }

def latest_base_time(now=None):
    now = now or datetime.now()
    slots = [2,5,8,11,14,17,20,23]
    # API 생성 지연 고려: 현재시각 10분 전 기준으로 가장 최근 발표시각 선택
    t = now - timedelta(minutes=10)
    candidates = [h for h in slots if h <= t.hour]
    if candidates:
        return t.strftime('%Y%m%d'), f'{max(candidates):02d}00'
    prev = t - timedelta(days=1)
    return prev.strftime('%Y%m%d'), '2300'


def forecast(region_key):
    require_key()
    info = REGIONS.get(region_key)
    if not info:
        raise RuntimeError('지원하지 않는 지역입니다.')
    nx, ny = dfs_xy(info['lat'], info['lon'])
    base_date, base_time = latest_base_time()
    params = {
        'serviceKey': normalize_key(SERVICE_KEY), 'pageNo': '1', 'numOfRows': '1000',
        'dataType': 'JSON', 'base_date': base_date, 'base_time': base_time,
        'nx': str(nx), 'ny': str(ny),
    }
    r = requests.get(FCST_URL, params=params, timeout=20)
    data = parse_kma_response(r, '단기예보 API')
    header = data.get('response', {}).get('header', {})
    if header.get('resultCode') not in ('00', '0'):
        raise RuntimeError(header.get('resultMsg', '단기예보 API 오류'))
    raw = data.get('response', {}).get('body', {}).get('items', {}).get('item', []) or []
    grouped = {}
    for x in raw:
        key = (x.get('fcstDate'), x.get('fcstTime'))
        grouped.setdefault(key, {})[x.get('category')] = x.get('fcstValue')
    out = []
    for (d,t), v in sorted(grouped.items()):
        out.append({
            'date': f'{d[:4]}-{d[4:6]}-{d[6:8]}' if d else '',
            'time': f'{t[:2]}:{t[2:4]}' if t else '',
            'pop': v.get('POP'), 'pty': v.get('PTY'), 'pcp': v.get('PCP'),
            'tmp': v.get('TMP'), 'wsd': v.get('WSD')
        })
    return out, nx, ny, base_date, base_time


@app.route('/')
def home():
    return send_from_directory(BASE_DIR, HTML_FILE)

@app.route('/api/kma/status')
def status():
    try:
        require_key()
        # 실제 AWS 일자료 API를 지역별 대표 관측지점에 대해 확인
        d = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
        checked = {}
        for k, v in REGIONS.items():
            aws_daily(d, d, v['stn'])
            checked[k] = {'name': v['station_name'], 'stn': v['stn']}
        return jsonify(ok=True, stations=checked)
    except Exception as e:
        print('[KMA STATUS ERROR]', repr(e), flush=True)
        return jsonify(ok=False, message=str(e)), 500

@app.route('/api/kma/daily')
def daily():
    try:
        start = request.args.get('start')
        end = request.args.get('end')
        threshold = float(request.args.get('threshold', '0.1'))
        if not start or not end:
            raise RuntimeError('start, end 날짜가 필요합니다.')

        # ASOS 일자료는 당일 자료를 제공하지 않고 전날(D-1)까지만 제공함.
        # 프런트에서 실수로 오늘 날짜를 보내더라도 서버가 자동으로 어제로 보정한다.
        try:
            start_dt = datetime.strptime(start, '%Y-%m-%d').date()
            end_dt = datetime.strptime(end, '%Y-%m-%d').date()
        except ValueError:
            raise RuntimeError('날짜 형식은 YYYY-MM-DD여야 합니다.')

        latest_available = (datetime.now() - timedelta(days=1)).date()
        if start_dt > latest_available:
            raise RuntimeError(f'ASOS 일자료는 {latest_available.isoformat()}까지 조회할 수 있습니다.')
        if end_dt > latest_available:
            end_dt = latest_available
        if start_dt > end_dt:
            raise RuntimeError('조회 시작일이 종료일보다 늦습니다.')

        actual_start = start_dt.isoformat()
        actual_end = end_dt.isoformat()

        results = []
        for info in REGIONS.values():
            items = aws_daily(actual_start, actual_end, info['stn'])
            latest, max_run, qualifying, current_run = dry_analysis(items, threshold)
            results.append({
                'region': info['name'], 'station': info['station_name'], 'status': 'ok',
                'latestRainfall': f'{latest:.1f} mm' if latest is not None else '-',
                'max': max_run, 'qualifying': qualifying, 'currentRun': current_run,
                'predictedDate': qualifying[0]['start'] if qualifying else None,
            })
        return jsonify(ok=True, results=results, start=actual_start, end=actual_end)
    except Exception as e:
        print('[KMA DAILY ERROR]', repr(e), flush=True)
        return jsonify(ok=False, message=str(e)), 500


@app.route('/api/kma/live')
def live():
    """세 대상 행정구역의 오늘 초단기실황을 반환합니다.
    주의: RN1은 최근 1시간 강수량이므로 '오늘 최종 일강수량'과는 다릅니다.
    """
    try:
        require_key()
        requested = request.args.get('region')
        keys = [requested] if requested else list(REGIONS.keys())
        results = []
        for key in keys:
            if key not in REGIONS:
                continue
            try:
                results.append(ultra_nowcast(key))
            except Exception as e:
                print(f'[KMA LIVE ERROR {key}]', repr(e), flush=True)
                results.append({
                    'regionKey': key, 'region': REGIONS[key]['name'],
                    'status': 'error', 'message': str(e)
                })
        any_ok = any(x.get('status') == 'ok' for x in results)
        return jsonify(ok=any_ok, results=results,
                       note='초단기실황 RN1은 최근 1시간 강수량이며 당일 확정 일강수량이 아닙니다.')
    except Exception as e:
        print('[KMA LIVE ERROR]', repr(e), flush=True)
        return jsonify(ok=False, message=str(e), results=[]), 500

@app.route('/api/kma/forecast')
def fcst():
    try:
        region = request.args.get('region', 'geumnam')
        items, nx, ny, base_date, base_time = forecast(region)
        return jsonify(ok=True, items=items, grid={'nx':nx,'ny':ny}, baseDate=base_date, baseTime=base_time)
    except Exception as e:
        print('[KMA FORECAST ERROR]', repr(e), flush=True)
        return jsonify(ok=False, message=str(e)), 500

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8000, debug=False)
