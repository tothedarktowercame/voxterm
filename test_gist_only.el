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
(kill-emacs (if (equal said '("Fixed. This is the line Joe must hear." "last line, no newline")) 0 1))
