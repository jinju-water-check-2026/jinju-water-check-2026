from flask import Flask, jsonify, request, send_from_directory
from urllib.parse import unquote
import re
import os, math, requests, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = 'index.html'
app = Flask(__name__, static_folder=None)

# 기상청 API허브 인증키는 Render 환경변수 KMA_SERVICE_KEY에 저장합니다.
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

    if not key or key == 'PASTE_YOUR_KMA_APIHUB_KEY_HERE':
        return ''
    return key

KMA_APIHUB_KEY = load_service_key()

def load_data_go_key():
    return os.environ.get('DATA_GO_KR_SERVICE_KEY', '').strip()

DATA_GO_KEY = load_data_go_key()

# 지역별 대표 방재기상관측(AWS)을 사용합니다.
# 금남면·진교면은 금남 AWS(933), 곤양면은 사천 AWS(917)을 대표 관측지점으로 지정합니다.
# AWS의 과거 일자료는 지역별 강수량/무강우 분석에 사용하고, 오늘 실황·미래 예보는 해당 관측지점 좌표의 대표 격자로 조회합니다.
REGIONS = {
    'geumnam': {'name': '하동군 금남면', 'stn': '933', 'station_name': '금남 AWS(933)', 'lat': 34.95056, 'lon': 127.85902},
    'jingyo':  {'name': '하동군 진교면', 'stn': '933', 'station_name': '금남 AWS(933)', 'lat': 34.95056, 'lon': 127.85902},
    'gonyang': {'name': '사천시 곤양면', 'stn': '917', 'station_name': '사천 AWS(917)', 'lat': 35.03695, 'lon': 128.06768},
}

AWS_DAILY_URL = 'https://apihub.kma.go.kr/api/typ01/url/sfc_aws_day.php'
AWS_HOURLY_URL = 'https://apihub.kma.go.kr/api/typ01/url/awsh.php'

FCST_URL = 'https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst'
NCST_URL = 'https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getUltraSrtNcst'


def require_key():
    if not KMA_APIHUB_KEY:
        raise RuntimeError('기상청 API허브 인증키가 설정되지 않았습니다. Render 환경변수 KMA_SERVICE_KEY를 확인해 주세요.')

def require_data_go_key():
    # 과거 호환용 이름은 유지하지만 예보도 이제 KMA API Hub 인증키를 사용합니다.
    require_key()


def now_kst():
    if ZoneInfo:
        return datetime.now(ZoneInfo('Asia/Seoul'))
    return datetime.now(timezone(timedelta(hours=9)))

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

def _split_kma_line(line):
    """KMA API Hub의 CSV/공백 구분 응답을 안전하게 분리합니다."""
    line = line.strip()
    if not line:
        return []
    if ',' in line:
        return [x.strip() for x in line.split(',')]
    return [x for x in re.split(r'\s+', line) if x]


def _find_kma_header(lines):
    """help=1 응답에서 실제 변수명 헤더를 찾습니다."""
    for ln in lines:
        h = ln.lstrip('#').strip()
        if re.match(r'^TM(?:[,\s]+)STN(?:[,\s]+)', h):
            return _split_kma_line(h)
    return []


def _parse_kma_csv(text):
    """KMA API Hub 응답을 최대한 안전하게 파싱합니다.

    disp=1을 사용하면 콤마 구분 CSV가 반환되므로 이를 우선 사용하고,
    혹시 고정폭 응답이 오더라도 헤더/필드명을 이용해 최대한 처리합니다.
    """
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    header = _find_kma_header(lines)
    rows = []
    for ln in lines:
        if ln.startswith('#'):
            continue
        parts = _split_kma_line(ln)
        if len(parts) < 2 or not re.match(r'^\d{8,14}$', parts[0]):
            continue
        stn = parts[1].strip()
        if not stn.isdigit():
            continue
        row = dict(zip(header, parts)) if header and len(parts) >= len(header) else {}
        rows.append(row | {'_parts': parts, 'tm': parts[0], 'stn': stn})
    return rows


def _row_value(row, *names, fallback_index=None):
    for name in names:
        for k, v in row.items():
            if str(k).upper() == name.upper() and v not in ('', None, '-9', '-99', '-999'):
                return v
    if fallback_index is not None:
        parts = row.get('_parts', [])
        if len(parts) > fallback_index:
            return parts[fallback_index]
    return None


def aws_daily(start_date, end_date, stn):
    """기상청 API허브 AWS 일강수량(rn_day) 조회."""
    require_key()
    params = {
        'tm1': start_date.replace('-', ''),
        'tm2': end_date.replace('-', ''),
        'obs': 'rn_day',
        'stn': str(stn),
        'disp': '1',
        'help': '1',
        'authKey': normalize_key(KMA_APIHUB_KEY),
    }
    r = requests.get(AWS_DAILY_URL, params=params, timeout=20)
    if not r.ok:
        raise RuntimeError(f'AWS 일자료 API허브 HTTP {r.status_code}: {(r.text or "")[:500]}')
    text = r.text or ''
    rows = _parse_kma_csv(text)
    items = []
    for row in rows:
        parts = row.get('_parts', [])
        # rn_day 요소 조회의 대표 응답은 TM, STN, LON, LAT, HT, VAL, STN_KO
        value = _row_value(row, 'VAL', 'RN_DAY', 'RN_DAY(mm)', fallback_index=5)
        station_name = _row_value(row, 'STN_KO', 'STN_NAME', fallback_index=6) or ''
        items.append({'tm': row['tm'], 'stn': row['stn'], 'sumRn': value, 'station_name': station_name})
    if not items:
        raise RuntimeError(f'AWS 일자료에서 {stn} 관측자료를 찾지 못했습니다. 응답: {text[:300]}')
    return items


def aws_hourly_snapshot(tm, stns):
    """AWS 정시자료를 한 번에 받아 여러 지점의 TA/HM/WS/RN_HR1을 추출합니다."""
    require_key()
    params = {
        'tm': tm, 'stn': ':'.join(map(str, stns)),
        'disp': '1', 'help': '1', 'authKey': normalize_key(KMA_APIHUB_KEY),
    }
    r = requests.get(AWS_HOURLY_URL, params=params, timeout=20)
    if not r.ok:
        raise RuntimeError(f'AWS 정시자료 HTTP {r.status_code}: {(r.text or "")[:500]}')
    rows = _parse_kma_csv(r.text or '')
    out = {}
    for row in rows:
        stn = row['stn']
        out[stn] = {
            'tm': row['tm'],
            'TA': _row_value(row, 'TA'),
            'HM': _row_value(row, 'HM'),
            'WS': _row_value(row, 'WS'),
            'RN_HR1': _row_value(row, 'RN_HR1'),
            'RN_DAY': _row_value(row, 'RN_DAY'),
        }
    return out

def dry_analysis(items, threshold=0.1):
    rows = []
    for x in items:
        raw_date = str(x.get('tm', ''))
        date = f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}' if len(raw_date) >= 8 and raw_date[:8].isdigit() else raw_date[:10]
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
    now = now or now_kst()
    # 정시 관측은 해당 시각 자료가 실제 생성되지 않았을 수 있으므로
    # 현재 시각과 직전 시각을 순서대로 시도할 수 있게 반환합니다.
    return now.replace(minute=0, second=0, microsecond=0)

def ultra_nowcast(region_key):
    """기상청 API Hub 동네예보 초단기실황(JSON)을 조회합니다."""
    require_key()
    info = REGIONS.get(region_key)
    if not info:
        raise RuntimeError('지원하지 않는 지역입니다.')
    nx, ny = dfs_xy(info['lat'], info['lon'])
    now = now_kst()
    candidates = [latest_ncst_base(now), latest_ncst_base(now) - timedelta(hours=1)]
    last_error = None
    for tm_dt in candidates:
        base_date = tm_dt.strftime('%Y%m%d')
        base_time = tm_dt.strftime('%H00')
        params = {
            'pageNo': '1', 'numOfRows': '1000', 'dataType': 'JSON',
            'base_date': base_date, 'base_time': base_time,
            'nx': str(nx), 'ny': str(ny), 'authKey': normalize_key(KMA_APIHUB_KEY),
        }
        try:
            r = requests.get(NCST_URL, params=params, timeout=20)
            data = parse_kma_response(r, '기상청 API허브 초단기실황')
            header = data.get('response', {}).get('header', {})
            if str(header.get('resultCode')) not in ('00','0'):
                raise RuntimeError(header.get('resultMsg') or '초단기실황 API 오류')
            raw = data.get('response', {}).get('body', {}).get('items', {}).get('item', []) or []
            vals = {str(x.get('category')): x.get('obsrValue') for x in raw}
            if not vals:
                raise RuntimeError(f'{base_date} {base_time} 초단기실황 자료가 없습니다.')
            return {
                'regionKey': region_key, 'region': info['name'], 'station': info['station_name'],
                'baseDate': base_date, 'baseTime': base_time,
                'rain1h': vals.get('RN1'), 'rainDay': None,
                'temp': vals.get('T1H'), 'humidity': vals.get('REH'), 'wind': vals.get('WSD'),
                'pty': vals.get('PTY'), 'status': 'ok', 'source': 'KMA API Hub 초단기실황',
            }
        except Exception as e:
            last_error = e
    raise RuntimeError(str(last_error) if last_error else '초단기실황 조회 실패')

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
    """기상청 API Hub 동네예보 단기예보(JSON)를 조회합니다."""
    require_key()
    info = REGIONS.get(region_key)
    if not info:
        raise RuntimeError('지원하지 않는 지역입니다.')
    nx, ny = dfs_xy(info['lat'], info['lon'])
    base_date, base_time = latest_base_time(now_kst())
    params = {
        'pageNo': '1', 'numOfRows': '1000', 'dataType': 'JSON',
        'base_date': base_date, 'base_time': base_time,
        'nx': str(nx), 'ny': str(ny), 'authKey': normalize_key(KMA_APIHUB_KEY),
    }
    r = requests.get(FCST_URL, params=params, timeout=20)
    data = parse_kma_response(r, '기상청 API허브 단기예보')
    header = data.get('response', {}).get('header', {})
    if str(header.get('resultCode')) not in ('00', '0'):
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
    if not out:
        raise RuntimeError(f'{base_date} {base_time} 단기예보 자료가 없습니다.')
    return out, nx, ny, base_date, base_time


@app.route('/')
def home():
    return send_from_directory(BASE_DIR, HTML_FILE)

@app.route('/api/kma/status')
def status():
    try:
        require_key()
        checked = {k: {'name': v['station_name'], 'stn': v['stn']} for k, v in REGIONS.items()}
        return jsonify(ok=True, api='KMA API Hub', stations=checked)
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

        # AWS 일자료는 관측자료 기준으로 안전하게 어제(D-1)까지만 조회합니다.
        try:
            start_dt = datetime.strptime(start, '%Y-%m-%d').date()
            end_dt = datetime.strptime(end, '%Y-%m-%d').date()
        except ValueError:
            raise RuntimeError('날짜 형식은 YYYY-MM-DD여야 합니다.')

        latest_available = (now_kst() - timedelta(days=1)).date()
        if start_dt > latest_available:
            raise RuntimeError(f'AWS 일자료는 {latest_available.isoformat()}까지 조회할 수 있습니다.')
        if end_dt > latest_available:
            end_dt = latest_available
        if start_dt > end_dt:
            raise RuntimeError('조회 시작일이 종료일보다 늦습니다.')

        actual_start = start_dt.isoformat()
        actual_end = end_dt.isoformat()

        results = []
        # 같은 AWS(933)를 사용하는 금남면/진교면은 한 번만 조회합니다.
        station_items = {}
        for stn in sorted({info['stn'] for info in REGIONS.values()}):
            station_items[stn] = aws_daily(actual_start, actual_end, stn)
        for info in REGIONS.values():
            items = station_items.get(info['stn'], [])
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
    """세 대상 행정구역의 오늘 초단기실황을 KMA API Hub로 자동 조회합니다."""
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
                results.append({'regionKey': key, 'region': REGIONS[key]['name'], 'station': REGIONS[key]['station_name'], 'status': 'error', 'message': str(e)})
        any_ok = any(x.get('status') == 'ok' for x in results)
        return jsonify(ok=any_ok, results=results, note='기상청 API Hub 초단기실황 RN1(최근 1시간 강수량)을 표시합니다.')
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
