#!/usr/bin/env bash
# 录制约 90 秒的五段真实界面演示。只监听 127.0.0.1，不对外网开放。
# 用法（在仓库根目录）：bash scripts/record_stage6_demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export POLICY_QA_HOME="${POLICY_QA_HOME:-/tmp/policy-qa-stage6-test}"
export DISPLAY="${DISPLAY:-:11}"

OUT_DIR="$ROOT/docs/demo"
RAW="/tmp/stage6/demo_raw.mp4"
FINAL="$OUT_DIR/policy-qa-demo-90s.mp4"
FLAG="/tmp/stage6/demo_done"
LOG_DIR="/tmp/stage6"
PORT="${STREAMLIT_PORT:-8501}"
API_PORT="${APP_PORT:-8000}"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

mkdir -p "$OUT_DIR" "$LOG_DIR" "$POLICY_QA_HOME"
rm -f "$FLAG" "$RAW"

echo "[record] POLICY_QA_HOME=${POLICY_QA_HOME} DISPLAY=${DISPLAY}"
echo "[record] 启动 python -m app.start（仅 127.0.0.1）"
"$PYTHON" -m app.start >"$LOG_DIR/app.log" 2>&1 &
APP_PID=$!

cleanup() {
  set +e
  if [[ -n "${FFMPEG_PID:-}" ]]; then kill -INT "$FFMPEG_PID" >/dev/null 2>&1; fi
  if [[ -n "${CHROME_PID:-}" ]]; then kill "$CHROME_PID" >/dev/null 2>&1; fi
  if [[ -n "${APP_PID:-}" ]]; then kill "$APP_PID" >/dev/null 2>&1; fi
  pkill -f "google-chrome.*127.0.0.1:${PORT}" >/dev/null 2>&1 || true
  # 再回收 app.start 拉起的子进程
  pkill -f "uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}" >/dev/null 2>&1 || true
  pkill -f "streamlit run .*streamlit_app.py .*--server.port ${PORT}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${PORT}" >/dev/null 2>&1 \
    && curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then
    echo "[record] 服务已就绪"
    break
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "[record] app.start 启动失败，见 $LOG_DIR/app.log" >&2
    tail -n 40 "$LOG_DIR/app.log" >&2 || true
    exit 1
  fi
  sleep 0.5
  if [[ "$i" -eq 90 ]]; then
    echo "[record] 等待服务超时，见 $LOG_DIR/app.log" >&2
    exit 1
  fi
done

PROFILE="/tmp/stage6/chrome-profile"
rm -rf "$PROFILE"
mkdir -p "$PROFILE"
CHROME_FLAGS=(
  --user-data-dir="$PROFILE"
  --no-first-run
  --no-default-browser-check
  --disable-infobars
  --disable-session-crashed-bubble
  --disable-features=Translate,TranslateUI,NotificationTriggers
  --disable-notifications
  --disable-save-password-bubble
  --no-sandbox
  --disable-dev-shm-usage
  --autoplay-policy=no-user-gesture-required
  --window-position=0,0
  --window-size=1920,1200
  --start-fullscreen
  --kiosk
)

echo "[record] 打开 Chrome 自动演示页"
DISPLAY="$DISPLAY" google-chrome "${CHROME_FLAGS[@]}" "http://127.0.0.1:${PORT}/?demo=1" \
  >"$LOG_DIR/chrome.log" 2>&1 &
CHROME_PID=$!
sleep 3

echo "[record] 开始录屏 $DISPLAY"
ffmpeg -y -hide_banner -loglevel warning \
  -f x11grab -video_size 1920x1200 -framerate 25 -i "${DISPLAY}.0" \
  -c:v libx264 -pix_fmt yuv420p -preset medium -crf 22 -movflags +faststart \
  "$RAW" >"$LOG_DIR/ffmpeg.log" 2>&1 &
FFMPEG_PID=$!
sleep 0.3

ok=0
for i in $(seq 1 300); do
  if [[ -f "$FLAG" ]]; then
    echo "[record] 演示结束，再多录 2 秒"
    sleep 2
    ok=1
    break
  fi
  if ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
    echo "[record] ffmpeg 意外退出，见 $LOG_DIR/ffmpeg.log" >&2
    cat "$LOG_DIR/ffmpeg.log" >&2 || true
    exit 1
  fi
  sleep 1
done
if [[ "$ok" -ne 1 ]]; then
  echo "[record] 等待演示结束超时，仍将裁切已录内容" >&2
fi

kill -INT "$FFMPEG_PID" >/dev/null 2>&1 || true
wait "$FFMPEG_PID" 2>/dev/null || true
FFMPEG_PID=""

if [[ ! -s "$RAW" ]]; then
  echo "[record] 未生成原始视频" >&2
  exit 1
fi

DUR="$("$PYTHON" - <<'PY'
import subprocess
raw="/tmp/stage6/demo_raw.mp4"
out=subprocess.check_output([
    "ffprobe","-v","error","-show_entries","format=duration",
    "-of","default=noprint_wrappers=1:nokey=1", raw
], text=True).strip()
print(out)
PY
)"
echo "[record] 原始时长 ${DUR}s"

FONT="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

"$PYTHON" - <<PY
import subprocess
from pathlib import Path

raw = Path("/tmp/stage6/demo_raw.mp4")
final = Path("$FINAL")
font = Path("$FONT")
dur = float("$DUR")
target = 90.0
# 目标约 90 秒。过短不拉伸；超过 98 秒按比例加快，最长加速到约 1.35x。
if dur <= 98:
    speed = 1.0
    print(f"[record] 时长 {dur:.2f}s，不加速")
else:
    speed = min(dur / target, 1.35)
    print(f"[record] 时长 {dur:.2f}s，加速 {speed:.3f}x 到 {dur/speed:.2f}s")

# 裁掉底部桌面面板，再在画面下方加一条说明，避免挡住引用。不写入密钥或本地密钥路径。
caption = "政策问答系统（轻量版）真实界面演示"
vf_parts = ["crop=1920:1159:0:0", "pad=1920:1219:0:0:color=0x103848"]
if font.is_file():
    safe = caption.replace("'", "’")
    vf_parts.append(
        f"drawtext=fontfile={font}:font=Noto Sans CJK SC:text='{safe}':"
        "x=(w-text_w)/2:y=h-44:fontsize=28:fontcolor=white"
    )
vf = ",".join(vf_parts)
if speed != 1.0:
    vf = f"{vf},setpts=PTS/{speed}"

cmd = [
    "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
    "-i", str(raw),
    "-vf", vf,
    "-an",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "23",
    "-movflags", "+faststart",
    str(final),
]
subprocess.check_call(cmd)
size = final.stat().st_size
print(f"[record] 写出 {final} ({size/1024/1024:.2f} MiB)")
if size > 45 * 1024 * 1024:
    print("[record] 文件偏大，再压一档")
    tmp = Path("/tmp/stage6/demo_small.mp4")
    subprocess.check_call([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(final),
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
        "-crf", "26", "-vf", "scale=1600:-2",
        "-movflags", "+faststart", str(tmp),
    ])
    tmp.replace(final)
    print(f"[record] 压缩后 {final.stat().st_size/1024/1024:.2f} MiB")
PY

echo "[record] 完成 $FINAL"
