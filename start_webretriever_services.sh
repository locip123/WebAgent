#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/tmp/service-logs"

declare -a MANAGED_PATTERNS=()
CLEANED_UP=0

XRAY_CONFIG="/usr/local/etc/xray/config.json"
CPA_CONFIG="/root/config.yaml"
KEEPER_DIR="/root/cpa-usage-keeper"
KEEPER_BIN="${KEEPER_DIR}/cpa-usage-keeper"

log() {
	printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

die() {
	log "错误：$*" >&2
	exit 1
}

is_running() {
	pgrep -f -- "$1" >/dev/null 2>&1
}

register_pattern() {
	local process_pattern="$1"
	local existing_pattern

	for existing_pattern in "${MANAGED_PATTERNS[@]}"; do
		if [[ "${existing_pattern}" == "${process_pattern}" ]]; then
			return 0
		fi
	done

	MANAGED_PATTERNS+=("${process_pattern}")
}

cleanup_services() {
	local process_pattern
	local remaining=0

	if (( CLEANED_UP == 1 )); then
		return 0
	fi
	CLEANED_UP=1
	trap - INT TERM

	log "正在停止受脚本管理的服务..."
	for process_pattern in "${MANAGED_PATTERNS[@]}"; do
		pkill -TERM -f -- "${process_pattern}" >/dev/null 2>&1 || true
	done

	for attempt in {1..20}; do
		remaining=0
		for process_pattern in "${MANAGED_PATTERNS[@]}"; do
			if is_running "${process_pattern}"; then
				remaining=1
				break
			fi
		done

		if (( remaining == 0 )); then
			break
		fi
		sleep 0.25
	done

	if (( remaining == 0 )); then
		log "所有受脚本管理的服务已停止。"
	else
		log "部分服务未能在 5 秒内退出，请检查相关进程。" >&2
	fi
}

handle_signal() {
	local exit_code="$1"
	cleanup_services
	exit "${exit_code}"
}

start_process() {
	local name="$1"
	local process_pattern="$2"
	local log_file="$3"
	shift 3
	register_pattern "${process_pattern}"

	if is_running "$process_pattern"; then
		log "${name} 已在运行，跳过启动。"
		return 0
	fi

	log "正在启动 ${name}..."
	"$@" >>"${log_file}" 2>&1 < /dev/null &
	local pid=$!
	echo "${pid}" > "${LOG_DIR}/${name}.pid"

	sleep 1
	if kill -0 "${pid}" 2>/dev/null; then
		log "${name} 启动成功，PID=${pid}。"
	else
		log "${name} 启动失败，请查看 ${log_file}。" >&2
		return 1
	fi
}

start_keeper() {
	local name="cpa-usage-keeper"
	local process_pattern="cpa-usage-keeper"
	local log_file="${LOG_DIR}/${name}.log"
	register_pattern "${process_pattern}"

	if is_running "${process_pattern}"; then
		log "${name} 已在运行，跳过启动。"
		return 0
	fi

	log "正在启动 ${name}..."
	(
		cd -- "${KEEPER_DIR}"
		exec ./cpa-usage-keeper >>"${log_file}" 2>&1 < /dev/null
	) &
	local pid=$!
	echo "${pid}" > "${LOG_DIR}/${name}.pid"

	sleep 1
	if kill -0 "${pid}" 2>/dev/null; then
		log "${name} 启动成功，PID=${pid}。"
	else
		log "${name} 启动失败，请查看 ${log_file}。" >&2
		return 1
	fi
}

trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM
trap 'exit_code=$?; cleanup_services; exit "${exit_code}"' EXIT

mkdir -p "${LOG_DIR}"

for required_command in xray cli-proxy-api google-chrome pgrep; do
	command -v "${required_command}" >/dev/null 2>&1 || \
		die "找不到命令：${required_command}"
done

[[ -f "${XRAY_CONFIG}" ]] || die "Xray 配置不存在：${XRAY_CONFIG}"
[[ -f "${CPA_CONFIG}" ]] || die "CPA 配置不存在：${CPA_CONFIG}"
[[ -x "${KEEPER_BIN}" ]] || die "keeper 不存在或不可执行：${KEEPER_BIN}"

start_process \
	"xray" \
	"xray run -config ${XRAY_CONFIG}" \
	"${LOG_DIR}/xray.log" \
	xray run -config "${XRAY_CONFIG}"

start_process \
	"cli-proxy-api" \
	"cli-proxy-api -config ${CPA_CONFIG}" \
	"${LOG_DIR}/cli-proxy-api.log" \
	cli-proxy-api -config "${CPA_CONFIG}"

start_keeper

for chrome_instance in 1 2 3; do
	case "${chrome_instance}" in
		1)
			port=9222
			profile_dir="${SCRIPT_DIR}/tmp/chrome-debug-profile"
			;;
		2)
			port=9223
			profile_dir="${SCRIPT_DIR}/tmp/chrome-debug-profile-2"
			;;
		3)
			port=9224
			profile_dir="${SCRIPT_DIR}/tmp/chrome-debug-profile-3"
		esac

	log_file="${LOG_DIR}/chrome-${port}.log"

	start_process \
		"chrome-${port}" \
		"google-chrome.*--remote-debugging-port=${port}" \
		"${log_file}" \
		google-chrome \
		--remote-debugging-port="${port}" \
		--user-data-dir="${profile_dir}" \
		--headless=new \
		--no-first-run \
		--no-sandbox
done

log "全部服务已启动，脚本保持前台运行。按 Ctrl+C 停止全部服务。"

while true; do
	sleep 5

	for process_pattern in "${MANAGED_PATTERNS[@]}"; do
		if ! is_running "${process_pattern}"; then
			log "检测到服务已退出：${process_pattern}" >&2
			exit 1
		fi
	done
done
