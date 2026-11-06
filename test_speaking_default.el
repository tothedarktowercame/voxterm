;;; test_speaking_default.el --- fresh loads speak by default -*- lexical-binding: t; -*-
;; Run: emacs -Q --batch -l voxterm.el -l test_speaking_default.el

(let ((fails 0))
  (unless voxterm-speak-stream
    (setq fails (1+ fails))
    (message "FAIL fresh default is silent"))
  (unless (advice-member-p #'voxterm--stream-advice 'agent-chat-stream-text)
    (setq fails (1+ fails))
    (message "FAIL stream advice absent after load"))
  (unless (advice-member-p #'voxterm--stream-end-advice
                           'agent-chat-end-streaming-message)
    (setq fails (1+ fails))
    (message "FAIL stream-end advice absent after load"))
  (voxterm--sync-speaking-advice)
  (unless (advice-member-p #'voxterm--stream-advice 'agent-chat-stream-text)
    (setq fails (1+ fails))
    (message "FAIL repeated synchronization removed advice"))
  (if (zerop fails)
      (message "test_speaking_default: all passed")
    (kill-emacs 1)))
