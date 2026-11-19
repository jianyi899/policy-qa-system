#!/usr/bin/env bash
# 录制约 90 秒的五段真实界面演示。只监听 127.0.0.1，不对外网开放。
# 用法（在仓库根目录）：bash scripts/record_stage7_demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-https://jp.cloud.langfuse.com}"
export DISPLAY="${DISPLAY:-:11}"

OUT_DIR="$ROOT/docs/demo"
RAW="/tmp/stage7/demo_raw.mp4"
FINAL="$OUT_DIR/policy-qa-demo-90s.mp4"
FLAG="/tmp/stage7/demo_done"
LOG_DIR="/tmp/stage7"
PORT=8501
PYTHON="$ROOT/.venv/bin/python"
STREAMLIT="$ROOT/.venv/bin/streamlit"

mkdir -p "$OUT_DIR" "$LOG_DIR"
rm -f "$FLAG" "$RAW"

echo "[record] 检查本机服务"
bash "$ROOT/scripts/start_local_services.sh" >/tmp/stage7/services.log 2>&1 || true

echo "[record] 启动 Streamlit（仅 127.0.0.1:$PORT）"
"$STREAMLIT" run "$ROOT/app/streamlit_app.py" \
  --server.address 127.0.0.1 \
  --server.port "$PORT" \
  --server.headless true \
  --browser.gatherUsageStats false \
  --server.runOnSave false \
  >"$LOG_DIR/streamlit.log" 2>&1 &
STREAMLIT_PID=$!
cleanup() {
  set +e
  if [[ -n "${FFMPEG_PID:-}" ]]; then kill "$FFMPEG_PID" >/dev/null 2>&1; fi
  if [[ -n "${CHROME_PID:-}" ]]; then kill "$CHROME_PID" >/dev/null 2>&1; fi
  if [[ -n "${STREAMLIT_PID:-}" ]]; then kill "$STREAMLIT_PID" >/dev/null 2>&1; fi
  pkill -f "google-chrome.*127.0.0.1:${PORT}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}" >/dev/null 2>&1; then
    echo "[record] Streamlit 已就绪"
    break
  fi
  if ! kill -0 "$STREAMLIT_PID" 2>/dev/null; then
    echo "[record] Streamlit 启动失败，见 $LOG_DIR/streamlit.log" >&2
    exit 1
  fi
  sleep 0.5
  if [[ "$i" -eq 60 ]]; then
    echo "[record] 等待 Streamlit 超时" >&2
    exit 1
  fi
done

# 先打开一次非自动页，让浏览器完成首屏渲染，再进入 demo=1。
PROFILE="/tmp/stage7/chrome-profile"
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
echo "[record] 开始录屏 $DISPLAY"
ffmpeg -y -hide_banner -loglevel warning \
  -f x11grab -video_size 1920x1200 -framerate 25 -i "${DISPLAY}.0" \
  -c:v libx264 -pix_fmt yuv420p -preset medium -crf 22 -movflags +faststart \
  "$RAW" >"$LOG_DIR/ffmpeg.log" 2>&1 &
FFMPEG_PID=$!
sleep 0.4
google-chrome "${CHROME_FLAGS[@]}" "http://127.0.0.1:${PORT}/?demo=1" \
  >"$LOG_DIR/chrome.log" 2>&1 &
CHROME_PID=$!

# 最长等 240 秒；演示脚本在五段结束后写 flag。
ok=0
for i in $(seq 1 240); do
  if [[ -f "$FLAG" ]]; then
    echo "[record] 演示结束，再多录 2 秒"
    sleep 2
    ok=1
    break
  fi
  if ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
    echo "[record] ffmpeg 意外退出，见 $LOG_DIR/ffmpeg.log" >&2
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
import subprocess,sys
raw="/tmp/stage7/demo_raw.mp4"
out=subprocess.check_output([
    "ffprobe","-v","error","-show_entries","format=duration",
    "-of","default=noprint_wrappers=1:nokey=1", raw
], text=True).strip()
print(out)
PY
)"
echo "[record] 原始时长 ${DUR}s"

"$PYTHON" - <<PY
import subprocess
from pathlib import Path

raw = Path("/tmp/stage7/demo_raw.mp4")
final = Path("$FINAL")
dur = float("$DUR")
# 目标约 90 秒。过短不拉伸；超过 95 秒按比例加快，最长加速到约 1.35x，避免文字完全看不清。
target = 90.0
if dur <= 95:
    speed = 1.0
    print(f"[record] 时长 {dur:.2f}s，不加速")
else:
    speed = min(dur / target, 1.35)
    print(f"[record] 时长 {dur:.2f}s，加速 {speed:.3f}x 到 {dur/speed:.2f}s")

cmd = [
    "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
    "-i", str(raw),
]
if speed != 1.0:
    cmd += ["-filter:v", f"setpts=PTS/{speed}"]
cmd += [
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
    tmp = Path("/tmp/stage7/demo_small.mp4")
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
