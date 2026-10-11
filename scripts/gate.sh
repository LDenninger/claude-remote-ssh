# Shell fast path of the remote-ssh hooks, sourced by every command in hooks/hooks.json with
# $e set to the hook event and $r to the plugin root. It starts Python only when work waits in
# one of this user's runtime directories, else drains stdin and exits, so a hook with nothing
# to do forks no interpreter. The runtime base rule mirrors state.runtime_base() exactly.
# e=base prints the base and exits.

b=${REMOTE_SSH_RUNTIME_BASE:-}
if [ -z "$b" ]; then
  if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    b=$XDG_RUNTIME_DIR/remote-ssh
  else
    case ${TMPDIR:-} in /var/folders/*) b=${TMPDIR%/}/rssh ;; esac
  fi
  if [ -z "$b" ] || [ ${#b} -gt 58 ]; then b=/tmp/remote-ssh-$(id -u); fi
fi
case $e in
  base) printf '%s\n' "$b"; exit 0 ;;
  PreToolUse) set -- "$b"/*/redirect-on ;;
  *) set -- "$b"/*/pending "$b"/*/context/*.md ;;
esac
for f do
  [ -e "$f" ] && exec python3 -S "$r/scripts/hook.py" "$e"
done
exec cat >/dev/null
