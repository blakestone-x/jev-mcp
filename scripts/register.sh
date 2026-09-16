#!/usr/bin/env bash
# bash 4+ required; do not run with sh

if [ -z "${BASH_VERSION:-}" ]; then
    printf '%s\n' 'bash 4+ required; do not run with sh' >&2
    exit 2
fi
if (( BASH_VERSINFO[0] < 4 )); then
    printf '%s\n' 'bash 4+ required; do not run with sh' >&2
    exit 2
fi

set -uo pipefail

repository="git+https://github.com/blakestone-x/jev-mcp@v0.2.1"
placeholder_api_key="..."
claude_command="claude mcp add --scope user jev -e TYPESAFE_API_KEY=${placeholder_api_key} -- uvx --from ${repository} jev-mcp"
codex_block="[mcp_servers.jev]
command = \"uvx\"
args = [\"--from\", \"${repository}\", \"jev-mcp\"]
env = { TYPESAFE_API_KEY = \"${placeholder_api_key}\" }"
generic_block="{
  \"mcpServers\": {
    \"jev\": {
      \"command\": \"uvx\",
      \"args\": [\"--from\", \"${repository}\", \"jev-mcp\"],
      \"env\": { \"TYPESAFE_API_KEY\": \"${placeholder_api_key}\" }
    }
  }
}"

if [[ "${1:-}" != "--apply" ]]; then
    printf '%s\n' "${claude_command}"
    printf '\n%s\n%s\n' "Codex configuration block:" "${codex_block}"
    printf '%s\n%s\n' "Generic MCP JSON block:" "${generic_block}"
    exit 0
fi

api_key="${TYPESAFE_API_KEY:-}"
if [[ -z "${api_key}" ]]; then
    printf '%s\n' 'TYPESAFE_API_KEY is not set; refusing to apply registration changes.' >&2
    exit 1
fi
if [[ "${api_key}" == *'"'* || "${api_key}" == *'\'* || "${api_key}" == *$'\n'* || "${api_key}" == *$'\r'* ]]; then
    printf '%s\n' 'TYPESAFE_API_KEY contains unsupported characters; refusing to apply registration changes.' >&2
    exit 1
fi

umask 077

codex_apply_block="[mcp_servers.jev]
command = \"uvx\"
args = [\"--from\", \"${repository}\", \"jev-mcp\"]
env = { TYPESAFE_API_KEY = \"${api_key}\" }"

claude_list=""
if claude_list="$(claude mcp list 2>&1)"; then
    :
else
    claude_list_status=$?
fi

if printf '%s\n' "${claude_list}" | grep -Eiq '(^|[[:space:]])jev([:[:space:]]|$)'; then
    claude_get_output=""
    if claude_get_output="$(claude mcp get jev 2>&1)"; then
        :
    else
        claude_get_status=$?
    fi
    if printf '%s\n' "${claude_get_output}" | grep -Eiq 'TYPESAFE_API_KEY[[:space:]]*(=|:)[[:space:]]*"?\.\.\.'; then
        claude_remove_output=""
        claude_remove_status=0
        if claude_remove_output="$(claude mcp remove jev 2>&1)"; then
            :
        else
            claude_remove_status=$?
        fi
        if (( claude_remove_status != 0 )); then
            printf '%s\n' 'claude mcp remove failed; refusing to replace the placeholder.' >&2
            exit "${claude_remove_status}"
        fi
        printf '%s\n' 'Claude configuration contained a placeholder; replacing jev registration.'
        has_jev=0
    else
        has_jev=1
    fi
else
    has_jev=0
fi

if (( has_jev == 1 )); then
    printf '%s\n' 'Claude configuration already contains jev; skipping add.'
else
    claude_add_output=""
    claude_add_status=0
    if claude_add_output="$(claude mcp add --scope user jev -e "TYPESAFE_API_KEY=${api_key}" -- uvx --from "${repository}" jev-mcp 2>&1)"; then
        :
    else
        claude_add_status=$?
    fi
    if (( claude_add_status != 0 )) && ! printf '%s\n' "${claude_add_output}" | grep -Eiq 'already exists'; then
        printf '%s\n' 'claude mcp add failed.' >&2
        exit "${claude_add_status}"
    fi
    if (( claude_add_status != 0 )); then
        printf '%s\n' 'Claude mcp add reported that jev already exists; continuing.'
    fi
fi

# A lookup is diagnostic only. Its non-zero status must never stop the script.
claude_get_status=0
claude mcp get jev >/dev/null 2>&1 || claude_get_status=$?
printf '%s\n' "Claude lookup completed with status ${claude_get_status}; continuing."

user_home="${HOME:-}"
if [[ -z "${user_home}" ]]; then
    printf '%s\n' 'HOME is not set; cannot update the Codex configuration.' >&2
    exit 1
fi
config_path="${user_home}/.codex/config.toml"
config_directory="${user_home}/.codex"
mkdir -p "${config_directory}"

commit_config_temp() {
    local temporary="$1"
    if [[ -f "${config_path}" ]]; then
        cp -- "${config_path}" "${config_path}.bak" || {
            rm -f -- "${temporary}"
            printf '%s\n' 'Could not create the Codex configuration backup; refusing to rewrite it.' >&2
            return 1
        }
        chmod 600 "${config_path}.bak" || {
            rm -f -- "${temporary}"
            printf '%s\n' 'Could not secure the Codex configuration backup; refusing to rewrite it.' >&2
            return 1
        }
    fi
    mv -- "${temporary}" "${config_path}" || {
        rm -f -- "${temporary}"
        printf '%s\n' 'Could not install the Codex configuration; refusing to continue.' >&2
        return 1
    }
    chmod 600 "${config_path}"
}

write_config_content() {
    local content="$1"
    local temporary
    temporary="$(mktemp "${config_path}.tmp.XXXXXX")" || {
        printf '%s\n' 'Could not create a temporary Codex configuration.' >&2
        return 1
    }
    if ! printf '%s' "${content}" > "${temporary}"; then
        rm -f -- "${temporary}"
        printf '%s\n' 'Could not write a temporary Codex configuration.' >&2
        return 1
    fi
    chmod 600 "${temporary}" || {
        rm -f -- "${temporary}"
        printf '%s\n' 'Could not secure the temporary Codex configuration.' >&2
        return 1
    }
    commit_config_temp "${temporary}"
}

placeholder_replaced=0
replace_placeholder_in_jev_section() {
    local source_path="$1"
    local target_path="$2"
    local line
    local in_jev=0
    local section_pattern='^\[([^]]+)\][[:space:]]*$'
    local placeholder_pattern='^(.*TYPESAFE_API_KEY[[:space:]]*=[[:space:]]*")\.\.\.(".*)$'
    placeholder_replaced=0
    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${line}" =~ ${section_pattern} ]]; then
            if [[ "${BASH_REMATCH[1]}" == 'mcp_servers.jev' ]]; then
                in_jev=1
            else
                in_jev=0
            fi
        fi
        if (( in_jev )) && [[ "${line}" =~ ${placeholder_pattern} ]]; then
            line="${BASH_REMATCH[1]}${api_key}${BASH_REMATCH[2]}"
            placeholder_replaced=1
        fi
        printf '%s\n' "${line}"
    done < "${source_path}" > "${target_path}"
}

if [[ -f "${config_path}" ]]; then
    temporary="$(mktemp "${config_path}.tmp.XXXXXX")" || {
        printf '%s\n' 'Could not create a temporary Codex configuration.' >&2
        exit 1
    }
    replace_placeholder_in_jev_section "${config_path}" "${temporary}"
    if (( placeholder_replaced )); then
        chmod 600 "${temporary}" || {
            rm -f -- "${temporary}"
            printf '%s\n' 'Could not secure the temporary Codex configuration.' >&2
            exit 1
        }
        commit_config_temp "${temporary}" || exit $?
        printf '%s\n' "Codex configuration placeholder replaced: ${config_path}"
    else
        rm -f -- "${temporary}"
        if grep -Eq '^\[mcp_servers\.jev\][[:space:]]*$' "${config_path}"; then
            chmod 600 "${config_path}"
            printf '%s\n' "Codex configuration already contains [mcp_servers.jev]: ${config_path}"
        else
            existing="$(<"${config_path}")"
            if [[ -s "${config_path}" ]] && [[ "$(tail -c 1 "${config_path}")" != $'\n' ]]; then
                existing+=$'\n'
            fi
            existing+="${codex_apply_block}"$'\n\n'
            write_config_content "${existing}" || exit $?
            printf '%s\n' "Codex configuration updated: ${config_path}"
        fi
    fi
else
    write_config_content "${codex_apply_block}"$'\n\n' || exit $?
    printf '%s\n' "Codex configuration updated: ${config_path}"
fi
