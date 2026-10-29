;;; test_gist_only.el --- voxterm-gist-only speaks the Gist wherever it appears -*- lexical-binding: t -*-
;; Run: emacs -Q --batch -l voxterm.el -l test_gist_only.el
;; bad case: narration first, tool lines, then a Gist late in the turn
(require 'cl-lib)
(defvar said nil)
(cl-letf (((symbol-function 'voxterm--post-say) (lambda (text &rest _) (push text said))))
  (with-temp-buffer
    (rename-buffer "*claude-repl:claude-17*" t)
    (let ((voxterm-speak-stream t) (voxterm-speak-only-buffer (buffer-name)) (voxterm-gist-only t))
      (voxterm--stream-advice "The cap is fixed. Recording the ruling:\n\n")
      (voxterm--stream-advice "[Bash] python3 - <<EOF\n")
      (voxterm--stream-advice "More narration here.\n\n")
      (voxterm--stream-advice "Gist: Fixed. This is the line Joe must hear.\n\nFor the screen: details.")
      (voxterm--stream-end-advice)
      (voxterm--stream-advice "A turn with no gist at all.\n\n")
      (voxterm--stream-end-advice)
      (voxterm--stream-advice "narration\n\nGist: last line, no newline")
      (voxterm--stream-end-advice))))
(setq said (nreverse said))
;; regression: default mode still speaks the narration that precedes a tool line
(defvar said2 nil)
(cl-letf (((symbol-function 'voxterm--post-say) (lambda (text &rest _) (push text said2))))
  (with-temp-buffer
    (let ((voxterm-speak-stream t) (voxterm-speak-only-buffer nil) (voxterm-gist-only nil))
      (voxterm--stream-advice "The cap is fixed. Recording the ruling:\n\n")
      (voxterm--stream-advice "[Bash] python3 - <<EOF\n")
      (voxterm--stream-end-advice))))
(message "default: %S" said2)
(unless (equal said2 '("The cap is fixed. Recording the ruling:")) (kill-emacs 1))
(message "%S" said)
(unless (equal said '("Fixed. This is the line Joe must hear." "A turn with no gist at all." "last line, no newline")) (kill-emacs 1))

;; bad case (2026-09-27, P3 lost): narration with no trailing newline, a tool line,
;; then a Gist chunk with no leading newline. Skipping the tool line must not glue
;; "…kind:" onto "Gist:" and hide it from the line-anchored match.
(defvar said3 nil)
(cl-letf (((symbol-function 'voxterm--post-say) (lambda (text &rest _) (push text said3))))
  (with-temp-buffer
    (let ((voxterm-speak-stream t) (voxterm-speak-only-buffer nil) (voxterm-gist-only t))
      (voxterm--stream-advice "Recording P3, with your additions:")
      (voxterm--stream-advice "\n[Bash] cd /home/joe/code/futon3c && cat >> x\n")
      (voxterm--stream-advice "Gist: P3 is recorded.\n\nFor the screen: details.")
      (voxterm--stream-end-advice))))
(message "glued: %S" said3)
(unless (equal said3 '("P3 is recorded.")) (kill-emacs 1))
;; bad case (2026-10-29, claude-16): a no-Gist turn whose tool preview runs to
;; several lines (a heredoc).  Only the first line carries "[Bash]"; the rest
;; must not be spoken as the turn's opening paragraph.
(defvar said4 nil)
(cl-letf (((symbol-function 'voxterm--post-say) (lambda (text &rest _) (push text said4))))
  (with-temp-buffer
    (let ((voxterm-speak-stream t) (voxterm-speak-only-buffer nil) (voxterm-gist-only t))
      (voxterm--stream-advice "\n[Bash] d=~/notes; python3 - <<'EOF'\nimport json,os\nd=os.path.expanduser(d)\n")
      (voxterm--stream-advice "Chunk 3 returned after 19 seconds.\n\nMore detail.")
      (voxterm--stream-end-advice))))
(message "multi-line tool: %S" said4)
(unless (equal said4 '("Chunk 3 returned after 19 seconds. More detail.")) (kill-emacs 1))
;; bad case (2026-10-29, claude-16 again): stray prose between tools, then the
;; reply.  The fallback must speak the reply, not the strays.
(defvar said5 nil)
(cl-letf (((symbol-function 'voxterm--post-say) (lambda (text &rest _) (push text said5))))
  (with-temp-buffer
    (let ((voxterm-speak-stream t) (voxterm-speak-only-buffer nil) (voxterm-gist-only t))
      (voxterm--stream-advice "\n[Bash] cd /x; W=w.py\n$W 17830 not-fixed\n")
      (voxterm--stream-advice "Server rate-limited; proceed with those I have.")
      (voxterm--stream-advice "\n[Agent] You are fork B.\n")
      (voxterm--stream-advice "Fork A: backfill ERT tests for bugs 808 3824 4792.")
      (voxterm--stream-advice "\n[Bash] cd /y\n")
      (voxterm--stream-advice "㊣ (\"don't count yet\") Agreed, and done.\n\nMore.")
      (voxterm--stream-end-advice))))
(message "strays: %S" said5)
(unless (equal said5 '("㊣ (\"don't count yet\") Agreed, and done. More.")) (kill-emacs 1))
(message "all gist-only cases ok")
(kill-emacs 0)
