;;; test_voice_conversation.el --- hear the Gist of the agent at the cursor -*- lexical-binding: t -*-
;; Run: emacs -Q --batch -l voxterm.el -l test_voice_conversation.el
;; Joe, 2026-10-28: "if that agent produces a gist, that gist would be read
;; aloud", for the buffer the cursor is in, switched on from the voxterm page.
(require 'cl-lib)
(defvar agent-chat--input-start nil)
(make-variable-buffer-local 'agent-chat--input-start)
(let ((a (get-buffer-create "*claude-repl:claude-15*"))
      (b (get-buffer-create "*claude-repl:claude-11*"))
      (fails 0) (said nil))
  (dolist (buf (list a b))
    (with-current-buffer buf (setq agent-chat--input-start (point-min-marker))))
  (cl-flet ((check (label got want)
              (unless (equal got want)
                (setq fails (1+ fails))
                (message "FAIL %s: got %S want %S" label got want)))
            (turn (buf &rest chunks)
              (with-current-buffer buf
                (dolist (c chunks) (voxterm--stream-advice c))
                (voxterm--stream-end-advice))))
    (cl-letf (((symbol-function 'voxterm--post-say) (lambda (text &rest _) (push text said))))
      (delete-other-windows)
      (set-window-buffer (selected-window) a)
      (let ((voxterm-pinned-buffer-name nil)
            (voxterm-speak-stream nil) (voxterm-speak-only-buffer nil)
            (voxterm-gist-only nil))
        (let ((state (voxterm-voice-conversation t)))
          (check "on: conversation" (plist-get state :conversation) t)
          (check "on: gist only" (plist-get state :gist-only) t)
          (check "on: hooked" (and (advice-member-p 'voxterm--stream-advice
                                                    'agent-chat-stream-text) t) t))

        ;; The proforma's shape: narration, a tool line, then the ㊥ paragraph.
        (turn a "Looking at how voxterm decides.\n" "[Bash] grep -n gist voxterm.el\n"
              "㊥ (\"read aloud\") It works now,\nacross two lines.\n\n㊢ (detail) More.")
        (check "the ㊥ paragraph is spoken, without mark or bracket, narration not"
               said '("It works now, across two lines."))
        (setq said nil)
        (turn a "Done.\n" "㊥ (status) Last paragraph of the stream")
        (check "a ㊥ paragraph ending the stream is spoken" said
               '("Last paragraph of the stream"))
        (setq said nil)
        (turn a "Gist: plain gist line.\n\nbody")
        (check "Gist: lines still work" said '("plain gist line."))

        (setq said nil)
        (turn b "㊥ claude-11 is not where the cursor is.\n\n")
        (check "a buffer away from the cursor stays silent" said nil)

        (setq said nil)
        (with-current-buffer a
          (voxterm--stream-advice "Working.\n")
          (set-window-buffer (selected-window) b)  ; Joe moves away mid-turn
          (voxterm--stream-advice "㊥ Still claude-15's turn.\n\n")
          (voxterm--stream-end-advice))
        (check "the turn belongs to the buffer that was the target when it began"
               said '("Still claude-15's turn."))

        (setq said nil)
        (turn b "㊥ Now the cursor is here.\n\n")
        (check "speech follows the cursor to the next turn" said
               '("Now the cursor is here."))

        (let ((state (voxterm-voice-conversation nil)))
          (check "off: not speaking" (plist-get state :speaking) nil)
          (check "off: unhooked" (advice-member-p 'voxterm--stream-advice
                                                  'agent-chat-stream-text) nil))
        (setq said nil)
        (turn a "㊥ Silent now.\n\n")
        (check "off means silent" said nil))))
  (if (zerop fails)
      (message "test_voice_conversation: all passed")
    (kill-emacs 1)))
