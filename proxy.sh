#!/usr/bin/env bash
set -euo pipefail

P_IP="${P_IP:-127.0.0.1}"
P_N="${P_N:-1}"
P_PORT0="${P_PORT0:-8100}"
D_IP="${D_IP:-127.0.0.1}"
D_N="${D_N:-1}"
D_PORT0="${D_PORT0:-8200}"
PROXY_HOST="${PROXY_HOST:-0.0.0.0}"
PROXY_PORT="${PROXY_PORT:-8081}"
WORKERS="${WORKERS:-1}"

prefiller_hosts=()
prefiller_ports=()
decoder_hosts=()
decoder_ports=()
for ((i=0; i<P_N; i++)); do
    prefiller_hosts+=("$P_IP")
    prefiller_ports+=("$((P_PORT0 + i))")
done
for ((i=0; i<D_N; i++)); do
    decoder_hosts+=("$D_IP")
    decoder_ports+=("$((D_PORT0 + i))")
done

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python "$script_dir/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py" \
    --host "$PROXY_HOST" --port "$PROXY_PORT" --workers "$WORKERS" \
    --prefiller-hosts "${prefiller_hosts[@]}" --prefiller-ports "${prefiller_ports[@]}" \
    --decoder-hosts "${decoder_hosts[@]}" --decoder-ports "${decoder_ports[@]}"
