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
    # The command is read line by line: L[k] is line k and LEN[k] its length.
    # The position is line K, column C, and column LEN[K] + 1 is the newline
    # that ends line K. Holding the command as one string was quadratic: in
    # original-awk, the codebase macOS awk comes from, each substr on a long
    # string costs time that grows with the string, and a commit with a
    # 1000-line quoted message took 11 s to read.
    function at() {
      if (K > NL) return ""
      return C <= LEN[K] ? substr(L[K], C, 1) : "\n"
    }
    function next1() {
      if (K > NL) return ""
      if (C < LEN[K]) return substr(L[K], C + 1, 1)
      if (C == LEN[K]) return "\n"
      if (K == NL) return ""
      return LEN[K + 1] ? substr(L[K + 1], 1, 1) : "\n"
    }
    function adv() { if (C <= LEN[K]) C++; else { K++; C = 1 } }

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

    # Text another command may run is checked as before: the old pattern,
    # line by line, the way grep read the whole command.
    function scan(t,    n, i, part) {
      SEEN_COMMIT = 0; SEEN_N = 0
      n = split(t, part, "\n")
      for (i = 1; i <= n; i++) {
        if (part[i] ~ commits) SEEN_COMMIT = 1
        if (part[i] ~ old) SEEN_N = 1
      }
    }
    function runs_a_bypass(t) { scan(t); return SEEN_COMMIT && SEEN_N }

    # Commands up to stop: a ")" or a backtick, or the end when stop is empty.
    # msg is 1 inside a command substitution among the words of a commit.
    function parse_list(stop, msg,    c) {
      while (K <= NL && FAIL == "") {
        c = at()
        if (c == stop) return
        if (c == "\n") { adv(); read_bodies(); continue }
        if (blank(c) || c == ";" || c == "&" || c == "|") { adv(); continue }
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
      while (K <= NL && FAIL == "") {
        c = at()
        if (blank(c)) { adv(); continue }
        if (c == "\\" && next1() == "\n") { adv(); adv(); continue }
        if (c == stop || c == "\n" || c == ";" || c == "|" || c == ")") break
        if (c == "&" && next1() != ">") break
        if (c == "#") { C = LEN[K] + 1; continue }
        if (c == "(") { adv(); parse_list(")", msg); adv(); continue }
        if ((c == "<" || c == ">") && next1() == "(") {
          adv(); adv(); parse_list(")", msg || commit); adv(); continue
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
      if (runs_a_bypass(text)) found()
      if (commit || SEEN_COMMIT) COMMIT = 1
      for (k = first; k <= HN; k++) HCMD[k] = text
    }

    # A redirection. A heredoc body is read after the end of its line. It is
    # message text when its delimiter is quoted and it feeds the commit itself
    # (own), or cat inside a command substitution among the commit words (msg).
    # A here-string that is not message text counts as one of the words.
    function redirect(stop, own, msg, cmd,    op, c, word, message) {
      op = at(); adv(); c = at()
      if (op == "<" && c == "<") {
        op = "<<"; adv()
        if (at() == "<") { op = "<<<"; adv() }
        else if (at() == "-") { op = "<<-"; adv() }
      } else if (op == "<" && (c == "&" || c == ">")) { op = op c; adv() }
      else if (op == ">" && (c == ">" || c == "&" || c == "|")) { op = op c; adv() }
      else if (op == "&") { op = "&>"; adv(); if (at() == ">") { op = "&>>"; adv() } }
      while (blank(at())) adv()
      word = read_word(stop, msg || own)
      message = own || (msg && cmd == "cat")
      if (op == "<<" || op == "<<-") {
        HN++; HDELIM[HN] = word; HSTRIP[HN] = (op == "<<-"); HTEXT[HN] = message && QUOTED
        HCMD[HN] = ""
      } else if (op == "<<<" && !message) HERE = HERE " " word
    }

    # Heredoc bodies, line by line, after the newline that ends their command.
    # A body that is not message text is checked with the words of the
    # command that reads it, which a shell reading it would see as arguments.
    function read_bodies(    k, line, m, b) {
      for (k = 1; k <= HN; k++) {
        m = 0; b = 0
        while (K <= NL) {
          line = L[K]; K++; C = 1
          if (HSTRIP[k]) sub(/^\t+/, "", line)
          if (line == HDELIM[k]) break
          if (!HTEXT[k]) { if (line ~ commits) m = 1; if (line ~ old) b = 1 }
        }
        if (!HTEXT[k]) {
          scan(HCMD[k])
          if (m || SEEN_COMMIT) COMMIT = 1
          if ((m || SEEN_COMMIT) && (b || SEEN_N)) found()
        }
      }
      HN = 0
    }

    # One word with its quotes removed. A command substitution is parsed for
    # the commands in it and adds nothing to the word: its output is unknown.
    # QUOTED says whether any part of the word was quoted.
    function read_word(stop, msg,    c, v, q, rest, e) {
      v = ""; q = 0
      while (K <= NL && FAIL == "") {
        c = at()
        if (blank(c) || c == "\n" || c == ";" || c == "&" || c == "|" \
          || c == "<" || c == ">" || c == "(" || c == ")") break
        if (c == "`") {
          if (stop == "`") break
          adv(); parse_list("`", msg); adv(); continue
        }
        if (c == "\\") {
          if (next1() == "\n") { adv(); adv(); continue }
          adv(); v = v at(); adv(); q = 1; continue
        }
        if (c == "\047") {
          adv(); q = 1
          while (1) {
            if (K > NL) { FAIL = "unclosed quote"; break }
            rest = substr(L[K], C); e = index(rest, "\047")
            if (e) { v = v substr(rest, 1, e - 1); C += e; break }
            v = v rest "\n"; K++; C = 1
          }
          continue
        }
        if (c == "\"") { v = v dquote(msg); q = 1; continue }
        if (c == "$" && next1() == "(") { adv(); adv(); parse_list(")", msg); adv(); continue }
        if (c == "$" && next1() == "\047") { adv(); v = v ansi_c(); q = 1; continue }
        if (c == "$" && next1() == "\"") { adv(); continue }
        if (c == "$" && next1() == "{") { v = v brace(); continue }
        v = v c; adv()
      }
      QUOTED = q
      return v
    }

    # Plain text inside the quotes is copied a run at a time, up to the next
    # character that means something there.
    function dquote(msg,    c, v, rest) {
      adv(); v = ""
      while (K <= NL && FAIL == "") {
        rest = substr(L[K], C)
        if (!match(rest, /["\\`$]/)) { v = v rest "\n"; K++; C = 1; continue }
        v = v substr(rest, 1, RSTART - 1); C += RSTART - 1
        c = at()
        if (c == "\"") { adv(); return v }
        if (c == "\\") {
          c = next1()
          if (c == "\n") { adv(); adv(); continue }
          if (c == "$" || c == "`" || c == "\"" || c == "\\") { v = v c; adv(); adv(); continue }
          v = v "\\"; adv(); continue
        }
        if (c == "`") { adv(); parse_list("`", msg); adv(); continue }
        if (c == "$" && next1() == "(") { adv(); adv(); parse_list(")", msg); adv(); continue }
        if (c == "$" && next1() == "{") { v = v brace(); continue }
        v = v c; adv()
      }
      if (FAIL == "") FAIL = "unclosed double quote"
      return v
    }

    function ansi_c(    c, v) {
      adv(); v = ""
      while (K <= NL) {
        c = at()
        if (c == "\047") { adv(); return v }
        if (c == "\\") { adv(); v = v at(); adv(); continue }
        v = v c; adv()
      }
      FAIL = "unclosed quote"
      return v
    }

    function brace(    depth, c, v) {
      v = "${"; adv(); adv(); depth = 1
      while (K <= NL) {
        c = at(); v = v c; adv()
        if (c == "\\") { v = v at(); adv() }
        else if (c == "{") depth++
        else if (c == "}" && --depth == 0) return v
      }
      FAIL = "unclosed ${"
      return v
    }

    { L[NR] = $0; LEN[NR] = length($0) }

    END {
      NL = NR; K = 1; C = 1
      parse_list("", 0)
      out = COMMIT ? " commit" : ""
      if (REUSE) out = out " reuse"
      if (VERDICT != "") out = out " " VERDICT
      else if (FAIL != "" || UNSURE) out = out " unread"
      if (out != "") print substr(out, 2)
    }'
}

# The reader runs on a command whose text holds commit. One that does not
# cannot name the subcommand unless a quote or a backslash splits the word,
# and skipping the reader there keeps its cost off most commands.
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
