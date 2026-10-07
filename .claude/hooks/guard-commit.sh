#!/usr/bin/env bash
# PreToolUse guard on Bash: commit contract. Hard denies (exit 2).
# Modeled on Sorrel's guard-commit.sh; resolution to an approved spec is CI's job.
set -u

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

deny() { printf '%s\n' "$1" >&2; exit 2; }

# A git commit as the whole string shows it, git and commit side by side.
COMMITS='(^|[^[:alnum:]_])git[[:space:]]+commit'

# A git commit with git's global options between git and commit, for text that
# is matched rather than read word by word (spec 045's amendment on git's
# global options). A unit of a word is a character that is not a space or a
# quote, or a quoted run. The options in VALUED take the next word as their
# value, as git 2.43 reads them. Any other option takes none, and a bare -C or
# -c is never read as one that takes none.
q="'"
UNIT="([^[:space:]\"$q]|\"[^\"]*\"|$q[^$q]*$q)"
VALUED="(-[Cc]|--(git-dir|work-tree|namespace|config-env|attr-source|shallow-file))"
GLOBAL_COMMITS="(^|[^[:alnum:]_])git([[:space:]]+$VALUED[[:space:]]+$UNIT+|[[:space:]]+-([^[:space:]Cc]|[Cc][^[:space:]])$UNIT*)*[[:space:]]+[\"$q]?commit"

# No bypassing the hooks.
#
# Only the words of each git commit are read, as the shell passes them to git
# (spec 045's amendment on the commit guard). Matching the whole string denied
# ordinary work, the -n of a sed or grep chained beside a commit or one in its
# message, and let through spellings that skip the hooks: a quoted "-n", an -n
# before a semicolon, -nm"msg", --no-veri, and a commit inside a subshell or a
# command substitution.
#
# A short cluster counts only up to the first option that takes a value,
# because git reads the rest as that value: -nm "msg" and -an skip the hooks,
# -mn is the message "n". The whole-string pattern below stays for what is not
# a commit's own words: another command whose text holds a git commit, such as
# bash -c "...", read together with its words, which that text may receive as
# arguments; a commit whose words hold "$@", which come from elsewhere; and a
# command the reader cannot read. Its single leading dash keeps --no-edit and
# --amend from matching.
OLD_BYPASS='--no-verify|(^|[[:space:]])-[A-Za-z]*n[A-Za-z]*([[:space:]]|=|$)'

# Prints, separated by spaces: "commit" when it reads a git commit, or text
# another command receives holds one; "reuse" when -C is one of a commit's own
# words; "bypass" when a git commit in the command skips the hooks, or else
# "unread" when the command cannot be read. Written to POSIX awk, since the
# guard runs under macOS awk as well as mawk and gawk. If awk itself fails, the
# whole string is checked as before.
commit_words() {
  printf '%s' "$CMD" | LC_ALL=C awk -v commits="$GLOBAL_COMMITS" -v old="$OLD_BYPASS" '
    function at(i) { return substr(S, i, 1) }
    function blank(c) { return c == " " || c == "\t" }
    function is_git(w) { return w == "git" || w ~ /\/git$/ }
    function found() { VERDICT = "bypass" }

    # A global option of git that takes the next word as its value, as git
    # 2.43 reads it. Written as one word with =, a long one takes none.
    function takes_value(w) {
      return w ~ /^(-C|-c|--git-dir|--work-tree|--namespace|--config-env|--attr-source|--shallow-file)$/
    }

    # --no-verify, an abbreviation git accepts, or a short cluster with an n
    # before the first option that takes a value.
    function skips_hooks(w,    i, c) {
      if (w ~ /^--no-veri/ && index("--no-verify", w) == 1) return 1
      if (w !~ /^-[^-]/) return 0
      for (i = 2; i <= length(w); i++) {
        c = substr(w, i, 1)
        if (c == "n") return 1
        if (index("mFcCtSu", c)) return 0
      }
      return 0
    }

    # Text another command may run is checked whole, as before.
    function runs_a_bypass(t) { return t ~ commits && t ~ old }

    # Commands up to stop: a ")" or a backtick, or the end when stop is empty.
    # msg is 1 inside a command substitution among the words of a commit.
    function parse_list(stop, msg,    c) {
      while (P <= N && FAIL == "") {
        c = at(P)
        if (c == stop) return
        if (c == "\n") { P++; read_bodies(); continue }
        if (blank(c) || c == ";" || c == "&" || c == "|") { P++; continue }
        if (c == ")") { FAIL = "unbalanced )"; return }
        parse_command(stop, msg)
      }
      if (stop != "" && FAIL == "") FAIL = "unclosed " stop
    }

    # One simple command. Its subcommand is the first word after git that is
    # neither a global option nor the value of one (the amendment to spec 045
    # on the global options of git). When that is commit, the words after it
    # are its options, unless they hold "$@" or another positional parameter:
    # then they come from elsewhere in the string, which is checked whole. Any
    # other command that receives git commit in its text is checked whole too,
    # its words and that text together, since the text may run with those
    # words as arguments.
    function parse_command(stop, msg,    c, nw, w, commit, g, value, i, first, outer, text, k) {
      nw = 0; commit = 0; g = 0; value = 0; first = HN + 1; outer = HERE; HERE = ""
      while (P <= N && FAIL == "") {
        c = at(P)
        if (blank(c)) { P++; continue }
        if (c == "\\" && at(P + 1) == "\n") { P += 2; continue }
        if (c == stop || c == "\n" || c == ";" || c == "|" || c == ")") break
        if (c == "&" && at(P + 1) != ">") break
        if (c == "#") { while (P <= N && at(P) != "\n") P++; continue }
        if (c == "(") { P++; parse_list(")", msg); P++; continue }
        if ((c == "<" || c == ">") && at(P + 1) == "(") {
          P += 2; parse_list(")", msg || commit); P++; continue
        }
        if (c == "<" || c == ">" || c == "&") { redirect(stop, commit > 0, msg, w[1]); continue }
        w[++nw] = read_word(stop, msg || commit)
        if (commit) continue
        if (!g) { if (is_git(w[nw])) g = nw }
        else if (value) value = 0
        else if (w[nw] ~ /^-/) value = takes_value(w[nw])
        else if (w[nw] == "commit") commit = nw
        else g = is_git(w[nw]) ? nw : 0
      }
      text = HERE; HERE = outer
      for (i = 1; i <= nw; i++) {
        if (commit && i > commit) {
          if (skips_hooks(w[i])) found()
          if (w[i] == "-C") REUSE = 1
          if (w[i] ~ /\$[@*0-9]|\$[{][@*0-9]/) UNSURE = 1
        } else if (!commit || i < g) text = text " " w[i]
      }
      if (commit || text ~ commits) COMMIT = 1
      if (runs_a_bypass(text)) found()
      for (k = first; k <= HN; k++) HCMD[k] = text
    }

    # A redirection. A heredoc body is read after the end of its line. It is
    # message text when its delimiter is quoted and it feeds the commit itself
    # (own), or cat inside a command substitution among the commit words (msg).
    # A here-string that is not message text counts as one of the words.
    function redirect(stop, own, msg, cmd,    op, c, word, message) {
      op = at(P); P++; c = at(P)
      if (op == "<" && c == "<") {
        op = "<<"; P++
        if (at(P) == "<") { op = "<<<"; P++ }
        else if (at(P) == "-") { op = "<<-"; P++ }
      } else if (op == "<" && (c == "&" || c == ">")) { op = op c; P++ }
      else if (op == ">" && (c == ">" || c == "&" || c == "|")) { op = op c; P++ }
      else if (op == "&") { op = "&>"; P++; if (at(P) == ">") { op = "&>>"; P++ } }
      while (blank(at(P))) P++
      word = read_word(stop, msg || own)
      message = own || (msg && cmd == "cat")
      if (op == "<<" || op == "<<-") {
        HN++; HDELIM[HN] = word; HSTRIP[HN] = (op == "<<-"); HTEXT[HN] = message && QUOTED
        HCMD[HN] = ""
      } else if (op == "<<<" && !message) HERE = HERE " " word
    }

    function read_bodies(    k, e, line, body, t) {
      for (k = 1; k <= HN; k++) {
        body = ""
        while (P <= N) {
          e = index(substr(S, P), "\n")
          line = substr(S, P, e - 1); P += e
          if (HSTRIP[k]) sub(/^\t+/, "", line)
          if (line == HDELIM[k]) break
          body = body line "\n"
        }
        # Checked with the words of the command that reads it, which a shell
        # reading it would see as arguments.
        if (HTEXT[k]) continue
        t = body " " HCMD[k]
        if (t ~ commits) COMMIT = 1
        if (runs_a_bypass(t)) found()
      }
      HN = 0
    }

    # One word with its quotes removed. A command substitution is parsed for
    # the commands in it and adds nothing to the word: its output is unknown.
    # QUOTED says whether any part of the word was quoted.
    function read_word(stop, msg,    c, v, q, e) {
      v = ""; q = 0
      while (P <= N && FAIL == "") {
        c = at(P)
        if (blank(c) || c == "\n" || c == ";" || c == "&" || c == "|" \
          || c == "<" || c == ">" || c == "(" || c == ")") break
        if (c == "`") {
          if (stop == "`") break
          P++; parse_list("`", msg); P++; continue
        }
        if (c == "\\") {
          if (at(P + 1) == "\n") { P += 2; continue }
          v = v at(P + 1); P += 2; q = 1; continue
        }
        if (c == "\047") {
          e = index(substr(S, P + 1), "\047")
          if (e == 0) { FAIL = "unclosed quote"; break }
          v = v substr(S, P + 1, e - 1); P += e + 1; q = 1; continue
        }
        if (c == "\"") { v = v dquote(msg); q = 1; continue }
        if (c == "$" && at(P + 1) == "(") { P += 2; parse_list(")", msg); P++; continue }
        if (c == "$" && at(P + 1) == "\047") { P++; v = v ansi_c(); q = 1; continue }
        if (c == "$" && at(P + 1) == "\"") { P++; continue }
        if (c == "$" && at(P + 1) == "{") { v = v brace(); continue }
        v = v c; P++
      }
      QUOTED = q
      return v
    }

    function dquote(msg,    c, v) {
      P++; v = ""
      while (P <= N && FAIL == "") {
        c = at(P)
        if (c == "\"") { P++; return v }
        if (c == "\\") {
          c = at(P + 1)
          if (c == "\n") { P += 2; continue }
          if (c == "$" || c == "`" || c == "\"" || c == "\\") { v = v c; P += 2; continue }
          v = v "\\"; P++; continue
        }
        if (c == "`") { P++; parse_list("`", msg); P++; continue }
        if (c == "$" && at(P + 1) == "(") { P += 2; parse_list(")", msg); P++; continue }
        if (c == "$" && at(P + 1) == "{") { v = v brace(); continue }
        v = v c; P++
      }
      if (FAIL == "") FAIL = "unclosed double quote"
      return v
    }

    function ansi_c(    c, v) {
      P++; v = ""
      while (P <= N) {
        c = at(P)
        if (c == "\047") { P++; return v }
        if (c == "\\") { v = v at(P + 1); P += 2; continue }
        v = v c; P++
      }
      FAIL = "unclosed quote"
      return v
    }

    function brace(    depth, c, v) {
      v = "${"; P += 2; depth = 1
      while (P <= N) {
        c = at(P); v = v c; P++
        if (c == "\\") { v = v at(P); P++ }
        else if (c == "{") depth++
        else if (c == "}" && --depth == 0) return v
      }
      FAIL = "unclosed ${"
      return v
    }

    { S = S $0 "\n" }

    END {
      N = length(S); P = 1
      parse_list("", 0)
      out = COMMIT ? " commit" : ""
      if (REUSE) out = out " reuse"
      if (VERDICT != "") out = out " " VERDICT
      else if (FAIL != "" || UNSURE) out = out " unread"
      if (out != "") print substr(out, 2)
    }'
}

# The reader runs on a command whose text holds commit. One that does not
# cannot name the subcommand unless a quote or a backslash splits the word, and
# the reader is not free: its cost grows faster than the command's length.
verdict=
case "$CMD" in
  *commit*) verdict=$(commit_words) || verdict=unread ;;
esac
says() { case " $verdict " in *" $1 "*) return 0 ;; esac; return 1; }

# A command commits when the whole string shows git and commit side by side,
# as before, or when the reader reads a commit in it. Past git's global
# options, and with either word quoted, only the reader sees one. When it
# cannot read the command, the whole string is matched past those options.
commits=no
if says commit || printf '%s' "$CMD" | grep -qE "$COMMITS" \
  || { says unread && printf '%s' "$CMD" | grep -qE "$GLOBAL_COMMITS"; }; then
  commits=yes
fi

# Never stage or commit env files (except templates).
if printf '%s' "$CMD" | grep -qE '(^|[^[:alnum:]_])git[[:space:]]+(add|commit)' \
  || [ "$commits" = yes ]; then
  if printf '%s' "$CMD" | grep -oE '\.env[A-Za-z0-9_.-]*' \
    | grep -vE '^\.env\.(example|sample|template)$' | grep -q .; then
    deny "BLOCKED: refusing to stage/commit .env* files. Credentials never enter git (ADR-002)."
  fi
fi

# Checked BEFORE the "is this a commit" gate below, for every command. It went
# there when that gate needed `git` and `commit` side by side, which let
# `git -c core.hooksPath=... commit` read as neither a commit nor a bypass.
# Matched as command tokens rather than anywhere in the string. The first
# version matched a bare substring, so it blocked any command whose text merely
# mentioned these, including the commit message describing this very guard
# (review of PR #50).
if printf '%s' "$CMD" | grep -qE '(^|[[:space:]])(-c[[:space:]]+core\.hooksPath|--git-dir|GIT_DIR)=?'; then
  deny "BLOCKED: redirecting hooksPath or the git dir disables the hook chain. The verification hooks ARE the definition of done."
fi

# Only inspect git commit commands from here on.
[ "$commits" = yes ] || exit 0

if says bypass || { says unread && printf '%s' "$CMD" | grep -qE -- "$OLD_BYPASS"; }; then
  deny "BLOCKED: 'git commit --no-verify' (or -n, including bundled forms like -nm) is not allowed. The verification hooks ARE the definition of done."
fi

# Amend without editing, or -C <commit>, reuses an already-trailered message.
# -C counts only among the commit's own words. Matched in the whole string, it
# was also git's global -C <path>, so every commit made with git -C would have
# skipped the trailer (spec 045's amendment on git's global options).
if printf '%s' "$CMD" | grep -qE -- '--amend[[:space:]]+--no-edit|--no-edit[[:space:]]+--amend' \
  || says reuse \
  || { says unread && printf '%s' "$CMD" | grep -qE -- '[[:space:]]-C[[:space:]]'; }; then
  exit 0
fi

# Every commit carries a Spec trailer. Shape only; CI resolves it.
if ! printf '%s' "$CMD" | grep -qE 'Spec:[[:space:]]*[0-9]{3}'; then
  deny "BLOCKED: commit is missing a 'Spec: NNN' trailer.

  git commit -m \"feat(tracker): add status transition tests\" -m \"Spec: 004\"

If no approved spec covers this work, write one first (use /spec) and wait for
approval. See .ai/rules/spec-approval.md."
fi

exit 0
