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

# A git commit with git's global options between git and commit, for a command
# the reader cannot read, matched whole (spec 045's amendment on git's global
# options). A unit of a word is a character that is not a space or a quote, or
# a quoted run. The options in VALUED take the next word as their value, as git
# 2.43 reads them. Any other option takes none, and a bare -C or -c is never
# read as one that takes none. The pattern can read a long one either way, and
# in mawk that cost time exponential in a run of them, so the reader reads text
# a word at a time instead (spec 045's amendment after review of PR #158). GNU
# grep, which matches this pattern here, read 4000 such options in 0.06 s.
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
# options; "envfile" when it reads an env file anywhere but in a commit's
# message; "bypass" when a git commit in the command skips the hooks, or else
# "unread" when the command cannot be read. Written to POSIX awk, since the
# guard runs under macOS awk as well as mawk and gawk. If awk itself fails, the
# whole string is checked as before.
commit_words() {
  printf '%s' "$CMD" | LC_ALL=C awk -v old="$OLD_BYPASS" '
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

    # A global option after which git runs no subcommand written later, as
    # git 2.43 reads it: help or version runs in its place, or git prints a
    # path or a list and exits.
    function runs_no_subcommand(w) {
      if (w ~ /^--exec-path/) return w !~ /^--exec-path=/
      return w ~ /^(-h|--help|-v|--version|--html-path|--man-path|--info-path|--list-cmds=.*)$/
    }

    # Whether git commit reads the word after option w as the value of w: w is
    # a short cluster whose first option that takes a value ends it, or a long
    # option that takes one, written without = and perhaps shortened.
    function commit_takes_value(w,    i, c, n, name) {
      if (w ~ /^--[^=]+$/) {
        n = split("author cleanup date file fixup message pathspec-from-file" \
          " reedit-message reuse-message squash template trailer", name, " ")
        for (i = 1; i <= n; i++) if (index("--" name[i], w) == 1) return 1
        return 0
      }
      if (w !~ /^-[^-]/) return 0
      for (i = 2; i <= length(w); i++) {
        c = substr(w, i, 1)
        if (index("mFcCt", c)) return i == length(w)
        if (index("Su", c)) return 0
      }
      return 0
    }

    # Where option w of git commit puts the value of option -s or --l: 2 when
    # git reads the next word as that value, after -s, a short cluster whose
    # first option that takes a value is an s that ends it, or --l written
    # without = and perhaps shortened; 1 when w holds the value itself, as
    # -m"text" and --message=text do, and VAL is then that value; 0 otherwise.
    function value_in(w, s, l,    i, c, e) {
      if (w ~ /^--[^=]+$/) return index("--" l, w) == 1 ? 2 : 0
      if (w ~ /^--[^=]+=/) {
        e = index(w, "="); VAL = substr(w, e + 1)
        return index("--" l, substr(w, 1, e - 1)) == 1
      }
      if (w !~ /^-[^-]/) return 0
      for (i = 2; i <= length(w); i++) {
        c = substr(w, i, 1)
        if (index("mFcCtSu", c)) { VAL = substr(w, i + 1); return c != s ? 0 : i == length(w) ? 2 : 1 }
      }
      return 0
    }

    # Whether the output of a command substitution is message text. msg is 1
    # inside a message and 0 outside one. It is 2 in an option of a commit,
    # where the text p before the substitution decides: an option whose
    # message is attached, as -m, -am and --message= are.
    function in_message(msg, p) {
      return msg == 2 ? value_in(p "x", "m", "message") == 1 : msg
    }

    # Whether text t names an env file: .env and the characters a file name
    # goes on with, other than the templates .env.example, .env.sample and
    # .env.template. The same test as the whole string check below.
    function names_env(t) {
      while (match(t, /\.env[A-Za-z0-9_.-]*/)) {
        if (substr(t, RSTART, RLENGTH) !~ /^\.env\.(example|sample|template)$/) return 1
        t = substr(t, RSTART + RLENGTH)
      }
      return 0
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

    # Whether a line of text holds a git commit: git, at the start or after a
    # character that is not a letter, a digit or _, then git global options
    # and the values of those that take one, then a commit that may be quoted.
    # It is read a word at a time, so each option is read one way. A pattern
    # that could read an option with or without a value took time exponential
    # in a run of them in mawk (the amendment to spec 045 after review of PR
    # #158). The line is split at blanks once, and a field that leaves a quote
    # open is joined to the fields after it until the quote closes. Reading
    # starts after every field that ends in such a git, quoted or not, as the
    # pattern did. A field already read in the same state, as an option or as
    # a value, ends the reading, which went no further the first time, so no
    # field is read more than twice.
    function holds_commit(t,    f, n, i, j, w, q, value, seen) {
      n = split(t, f, /[[:space:]]+/)
      for (i = 1; i < n; i++) {
        if (f[i] !~ /(^|[^[:alnum:]_])git$/) continue
        value = 0
        for (j = i + 1; j <= n && !seen[j, value]++; ) {
          if (!value && f[j] ~ /^["\047]?commit/) return 1
          w = f[j]; q = left_open(w, ""); j++
          while (q != "" && j <= n) { w = w " " f[j]; q = left_open(f[j], q); j++ }
          if (w == "" || q != "") break
          if (value) value = 0
          else if (w !~ /^-/ || runs_no_subcommand(w)) break
          else value = takes_value(w)
        }
      }
      return 0
    }

    # The quote that text t leaves open, given quote q open at its start, or
    # "" for none. A double quote pairs with the next double quote and a
    # single quote with the next single quote, and each pair encloses the
    # other kind.
    function left_open(t, q,    e) {
      while (1) {
        if (q != "") {
          e = index(t, q)
          if (!e) return q
          t = substr(t, e + 1); q = ""
        }
        if (!match(t, /["\047]/)) return ""
        q = substr(t, RSTART, 1); t = substr(t, RSTART + 1)
      }
    }

    # Text another command may run is checked line by line, the way grep read
    # the whole command: for a commit as above, and for an -n with the old
    # pattern.
    function scan(t,    n, i, part) {
      SEEN_COMMIT = 0; SEEN_N = 0
      n = split(t, part, "\n")
      for (i = 1; i <= n; i++) {
        if (!SEEN_COMMIT && holds_commit(part[i])) SEEN_COMMIT = 1
        if (part[i] ~ old) SEEN_N = 1
      }
    }
    function runs_a_bypass(t) { scan(t); return SEEN_COMMIT && SEEN_N }

    # Commands up to stop: a ")" or a backtick, or the end when stop is empty.
    # msg is 1 inside a command substitution whose output is message text.
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
    # on the global options of git), and there is none after an option that
    # runs no subcommand. When that is commit, the words after it are its
    # options, unless they hold "$@" or another positional parameter: then
    # they come from elsewhere in the string, which is checked whole. Any other
    # command that receives git commit in its text is checked whole too, its
    # words and that text together, since the text may run with those words
    # as arguments. A -C reuses a message only where git reads it as an
    # option: not as the value of another option, nor as a path after --.
    # Every word is checked for an env file except the message of a commit,
    # the value of -m or --message (the amendment to spec 045 on the env file
    # check). Only the output of a command substitution in that message is
    # message text. In any other word it is a word: a file -F reads, a path,
    # a file cat reads. So is the output of a subshell or of a process
    # substitution, which this does not follow.
    #
    # A heredoc or here-string fed to the command may be message text, and is
    # once the command ends: for a commit, when it reads its message from
    # stdin by -F - and no pathspec from there; for cat in a substitution whose
    # output is message text, when what cat writes is that output, not piped
    # on or redirected.
    function parse_command(stop, msg,    c, nw, w, commit, g, value, i, first, outer, text, k, r,
        arg, paths, mnext, fnext, pnext, fin, pin, mtext, val, path, piped, out, nh, hd, hg, ns, hs, lead) {
      nw = 0; commit = 0; g = 0; value = 0; first = HN + 1; outer = HERE; HERE = ""
      arg = 0; paths = 0; mnext = 0; fnext = 0; pnext = 0; fin = 0; pin = 0
      piped = 0; out = 0; nh = 0; ns = 0
      while (K <= NL && FAIL == "") {
        c = at()
        if (blank(c)) { adv(); continue }
        if (c == "\\" && next1() == "\n") { adv(); adv(); continue }
        if (c == "|") { piped = next1() != "|"; break }
        if (c == stop || c == "\n" || c == ";" || c == ")") break
        if (c == "&" && next1() != ">") break
        if (c == "#") { C = LEN[K] + 1; continue }
        if (c == "(") { adv(); parse_list(")", 0); adv(); continue }
        if ((c == "<" || c == ">") && next1() == "(") {
          adv(); adv(); parse_list(")", 0); adv(); continue
        }
        if (c == "<" || c == ">" || c == "&") {
          r = redirect(stop, commit > 0, msg, w[1])
          if (r == "out") out = 1
          else if (r == "heredoc") { hd[++nh] = HN; hg[nh] = GEN }
          else if (r == "here-string") hs[++ns] = WORD
          continue
        }
        if (!commit) {
          w[++nw] = read_word(stop, 0)
          if (!g) { if (is_git(w[nw])) g = nw }
          else if (value) value = 0
          else if (runs_no_subcommand(w[nw])) g = 0
          else if (w[nw] ~ /^-/) value = takes_value(w[nw])
          else if (w[nw] == "commit") commit = nw
          else g = is_git(w[nw]) ? nw : 0
          continue
        }
        # A word of the commit: the value of the option before it, a path
        # after --, or an option.
        w[++nw] = read_word(stop, arg ? mnext : paths ? 0 : 2)
        val[nw] = arg; path[nw] = paths; lead[nw] = WSUBLEAD
        if (arg) {
          mtext[nw] = mnext
          if (w[nw] == "-") { if (fnext) fin = 1; if (pnext) pin = 1 }
          arg = 0; mnext = 0; fnext = 0; pnext = 0
        } else if (w[nw] == "--") paths = 1
        else if (!paths) {
          arg = commit_takes_value(w[nw])
          r = value_in(w[nw], "m", "message"); mnext = (r == 2); mtext[nw] = (r == 1)
          r = value_in(w[nw], "F", "file"); fnext = (r == 2); if (r == 1 && VAL == "-") fin = 1
          r = value_in(w[nw], "", "pathspec-from-file"); pnext = (r == 2); if (r == 1 && VAL == "-") pin = 1
        }
      }
      r = commit ? (fin && !pin) : (!piped && !out)
      for (k = 1; k <= nh; k++) if (hg[k] == GEN) HTEXT[hd[k]] = r
      text = HERE; HERE = outer
      for (k = 1; k <= ns; k++) if (!r) { text = text " " hs[k]; if (names_env(hs[k])) ENV = 1 }
      for (i = 1; i <= nw; i++) {
        if (commit && i > commit) {
          if (skips_hooks(w[i])) found()
          if (w[i] == "-C" && !val[i] && !path[i]) REUSE = 1
          if (w[i] ~ /\$[@*0-9]|\$[{][@*0-9]/) UNSURE = 1
          if (lead[i] && !val[i] && !path[i]) UNSURE = 1
        } else if (!commit || i < g) text = text " " w[i]
        if (!mtext[i] && names_env(w[i])) ENV = 1
      }
      if (runs_a_bypass(text)) found()
      if (commit || SEEN_COMMIT) COMMIT = 1
      for (k = first; k <= HN; k++) HCMD[k] = text
    }

    # A redirection. A heredoc body is read after the end of its line. One
    # with a quoted delimiter that feeds the commit itself (own) or cat in a
    # substitution whose output is message text (msg) may be message text,
    # and so may a here-string fed to either: the command decides when it
    # ends. A here-string that is not message text counts as one of the
    # words. The target is never message text, nor a substitution in it, and
    # unless a heredoc delimiter it is checked for an env file, so a file read
    # into the message by < is. Returns "heredoc" or "here-string" for one
    # that may be message text, the here-string then in WORD, "out" for a
    # redirection of output, and "" otherwise.
    function redirect(stop, own, msg, cmd,    op, c, word, may) {
      op = at(); adv(); c = at()
      if (op == "<" && c == "<") {
        op = "<<"; adv()
        if (at() == "<") { op = "<<<"; adv() }
        else if (at() == "-") { op = "<<-"; adv() }
      } else if (op == "<" && (c == "&" || c == ">")) { op = op c; adv() }
      else if (op == ">" && (c == ">" || c == "&" || c == "|")) { op = op c; adv() }
      else if (op == "&") { op = "&>"; adv(); if (at() == ">") { op = "&>>"; adv() } }
      while (blank(at())) adv()
      word = read_word(stop, 0)
      may = own || (msg && cmd == "cat")
      if (op == "<<" || op == "<<-") {
        HN++; HDELIM[HN] = word; HSTRIP[HN] = (op == "<<-"); HTEXT[HN] = 0
        HCMD[HN] = ""
        return may && QUOTED ? "heredoc" : ""
      }
      if (op == "<<<" && may) { WORD = word; return "here-string" }
      if (op == "<<<") HERE = HERE " " word
      if (names_env(word)) ENV = 1
      return op ~ /^[>&]/ ? "out" : ""
    }

    # Heredoc bodies, line by line, after the newline that ends their command.
    # A body that is not message text is checked with the words of the
    # command that reads it, which a shell reading it would see as arguments,
    # and for an env file.
    function read_bodies(    k, line, m, b) {
      for (k = 1; k <= HN; k++) {
        m = 0; b = 0
        while (K <= NL) {
          line = L[K]; K++; C = 1
          if (HSTRIP[k]) sub(/^\t+/, "", line)
          if (line == HDELIM[k]) break
          if (!HTEXT[k]) {
            if (!m && holds_commit(line)) m = 1
            if (line ~ old) b = 1
            if (names_env(line)) ENV = 1
          }
        }
        if (!HTEXT[k]) {
          scan(HCMD[k])
          if (m || SEEN_COMMIT) COMMIT = 1
          if ((m || SEEN_COMMIT) && (b || SEEN_N)) found()
        }
      }
      HN = 0; GEN++
    }

    # One word with its quotes removed. A command substitution is parsed for
    # the commands in it and adds nothing to the word: its output is unknown.
    # QUOTED says whether any part of the word was quoted.
    # A word leads with a command substitution when the first one, at this
    # level, has only empty or dash text before it: git then reads the
    # substitution output as the start of the argument, which may be an option
    # such as -n (the amendment to spec 045 on a command substitution among
    # the options of a commit). A later substitution, or one behind a non-dash
    # literal, cannot introduce a leading dash, so it does not count.
    #
    # seen and lead are local, so a nested read_word for a substitution inside
    # this word does not clobber them. The globals WSUBSEEN and WSUBLEAD carry
    # the result to parse_command: each function writes them only at its own
    # return, so the outer read_word, which returns last, has the final word.
    # dquote reports through them the same way, folded in after its call.
    function read_word(stop, msg,    c, v, q, rest, e, seen, lead) {
      v = ""; q = 0; seen = 0; lead = 0
      while (K <= NL && FAIL == "") {
        c = at()
        if (blank(c) || c == "\n" || c == ";" || c == "&" || c == "|" \
          || c == "<" || c == ">" || c == "(" || c == ")") break
        if (c == "`") {
          if (stop == "`") break
          if (!seen) { seen = 1; lead = (v ~ /^-*$/) }
          adv(); parse_list("`", in_message(msg, v)); adv(); continue
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
        if (c == "\"") {
          v = v dquote(msg, v); q = 1
          if (!seen && DQSEEN) { seen = 1; lead = DQLEAD }
          continue
        }
        if (c == "$" && next1() == "(") {
          if (!seen) { seen = 1; lead = (v ~ /^-*$/) }
          adv(); adv(); parse_list(")", in_message(msg, v)); adv(); continue
        }
        if (c == "$" && next1() == "\047") { adv(); v = v ansi_c(); q = 1; continue }
        if (c == "$" && next1() == "\"") { adv(); continue }
        if (c == "$" && next1() == "{") { v = v brace(); continue }
        v = v c; adv()
      }
      QUOTED = q; WSUBSEEN = seen; WSUBLEAD = lead
      return v
    }

    # Plain text inside the quotes is copied a run at a time, up to the next
    # character that means something there. DQSEEN and DQLEAD report a leading
    # substitution to read_word, set only at return so a nested read_word does
    # not clobber them.
    function dquote(msg, p,    c, v, rest, seen, lead) {
      adv(); v = ""; seen = 0; lead = 0
      while (K <= NL && FAIL == "") {
        rest = substr(L[K], C)
        if (!match(rest, /["\\`$]/)) { v = v rest "\n"; K++; C = 1; continue }
        v = v substr(rest, 1, RSTART - 1); C += RSTART - 1
        c = at()
        if (c == "\"") { adv(); DQSEEN = seen; DQLEAD = lead; return v }
        if (c == "\\") {
          c = next1()
          if (c == "\n") { adv(); adv(); continue }
          if (c == "$" || c == "`" || c == "\"" || c == "\\") { v = v c; adv(); adv(); continue }
          v = v "\\"; adv(); continue
        }
        if (c == "`") {
          if (!seen) { seen = 1; lead = ((p v) ~ /^-*$/) }
          adv(); parse_list("`", in_message(msg, p v)); adv(); continue
        }
        if (c == "$" && next1() == "(") {
          if (!seen) { seen = 1; lead = ((p v) ~ /^-*$/) }
          adv(); adv(); parse_list(")", in_message(msg, p v)); adv(); continue
        }
        if (c == "$" && next1() == "{") { v = v brace(); continue }
        v = v c; adv()
      }
      if (FAIL == "") FAIL = "unclosed double quote"
      DQSEEN = seen; DQLEAD = lead
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

    # A parameter expansion, kept as text. A command substitution inside one
    # is not read, though the shell may run it, so a command that holds one
    # is checked whole (the amendment to spec 045 on the env file check).
    function brace(    depth, c, v) {
      v = "${"; adv(); adv(); depth = 1
      while (K <= NL) {
        c = at(); v = v c; adv()
        if (c == "\\") { v = v at(); adv() }
        else if (c == "{") depth++
        else if (c == "}" && --depth == 0) {
          if (index(v, "$(") || index(v, "`")) UNSURE = 1
          return v
        }
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
      if (ENV) out = out " envfile"
      if (VERDICT != "") out = out " " VERDICT
      else if (FAIL != "" || UNSURE) out = out " unread"
      if (out != "") print substr(out, 2)
    }'
}

# Checked for every command, before the reader and the "is this a commit"
# gate below. It went before that gate when the gate needed `git` and `commit`
# side by side, which let `git -c core.hooksPath=... commit` read as neither a
# commit nor a bypass. It needs no reader, so a reader that is slow or never
# returns does not hold it up (spec 045's amendment after review of PR #158).
# Matched as command tokens rather than anywhere in the string. The first
# version matched a bare substring, so it blocked any command whose text
# merely mentioned these, including the commit message describing this very
# guard (review of PR #50).
if printf '%s' "$CMD" | grep -qE '(^|[[:space:]])(-c[[:space:]]+core\.hooksPath|--git-dir|GIT_DIR)=?'; then
  deny "BLOCKED: redirecting hooksPath or the git dir disables the hook chain. The verification hooks ARE the definition of done."
fi

# The reader runs on a command whose text holds commit. One that does not
# cannot name the subcommand unless a quote or a backslash splits the word,
# and skipping the reader there keeps its cost off most commands.
reader=no
verdict=
case "$CMD" in
  *commit*) reader=yes; verdict=$(commit_words) || verdict=unread ;;
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

# Never stage or commit env files (except templates): checked for a git add or
# a git commit side by side, and for a commit the reader reads. Where the
# reader read the command, it reports an env file named anywhere but in a
# commit's message, so a message may name one (spec 045's amendment on the
# env file check). Where it did not run, or could not read the command, the
# whole string is checked, message and all.
names_env_file() {
  printf '%s' "$CMD" | grep -oE '\.env[A-Za-z0-9_.-]*' \
    | grep -vE '^\.env\.(example|sample|template)$' | grep -q .
}
ENV_FILE="BLOCKED: refusing to stage/commit .env* files. Credentials never enter git (ADR-002)."
if printf '%s' "$CMD" | grep -qE '(^|[^[:alnum:]_])git[[:space:]]+(add|commit)' \
  || [ "$commits" = yes ]; then
  if [ "$reader" = yes ] && ! says unread && ! says bypass; then
    if says envfile; then deny "$ENV_FILE"; fi
  elif names_env_file; then
    deny "$ENV_FILE"
  fi
fi

# Only inspect git commit commands from here on.
[ "$commits" = yes ] || exit 0

if says bypass || { says unread && printf '%s' "$CMD" | grep -qE -- "$OLD_BYPASS"; }; then
  deny "BLOCKED: 'git commit --no-verify' (or -n, including bundled forms like -nm) is not allowed. The verification hooks ARE the definition of done."
fi

# Amend without editing, or -C <commit>, reuses an already-trailered message.
# -C counts only among the commit's own words. Matched in the whole string, it
# was also git's global -C <path>, so every commit made with git -C would have
# skipped the trailer (spec 045's amendment on git's global options). Among
# those words it counts only where git reads it as an option, not as the value
# of -m or the like, nor as a path after `--` (the amendment after review of
# PR #158).
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
