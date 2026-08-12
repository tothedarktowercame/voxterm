;;; voxterm.el --- receive dictated text from the voxterm server -*- lexical-binding: t; -*-

;; Loaded on demand by voxterm's server.py via emacsclient; nothing to add to init.el.
;; Text arrives as a plain elisp call, so Emacs needs no listener of its own.

;;; Code:

(defgroup voxterm nil
  "Dictated text arriving from the voxterm whisper server."
  :group 'external)

(defcustom voxterm-fallback-buffer "*voxterm*"
  "Buffer used when the active buffer cannot accept an insertion."
  :type 'string :group 'voxterm)

(defcustom voxterm-space-before t
  "Insert a separating space when point is mid-line and not already after whitespace."
  :type 'boolean :group 'voxterm)

(defvar voxterm-after-insert-hook nil
  "Run in the target buffer after text is inserted, before any submit.")

(defun voxterm--submit-here ()
  "Run whatever RET is bound to in the current buffer; return a label.
Calling the command is deliberate: in `claude-repl-mode' RET is bound to
`claude-repl-send-input', and invoking it directly is more reliable than
synthesising a keypress from a daemon eval."
  (let ((cmd (key-binding (kbd "RET"))))
    (if (commandp cmd)
        (progn (call-interactively cmd) (format "%s" cmd))
      (progn (insert "\n") "newline"))))

(defun voxterm--target-window ()
  "The window a dictation should land in: selected window of a visible frame."
  (let ((frame (or (car (filtered-frame-list
                         (lambda (f)
                           (and (frame-visible-p f)
                                (not (frame-parameter f 'voxterm-ignore))))))
                   (selected-frame))))
    (frame-selected-window frame)))

(defun voxterm--writable-p (buf)
  (and (buffer-live-p buf)
       (with-current-buffer buf
         (and (not buffer-read-only)
              (not (minibufferp))))))

;;;###autoload
(defun voxterm-target-name ()
  "Return the buffer a dictation would land in right now, without inserting.
Handy for checking the wiring before letting it write anywhere."
  (let* ((win (voxterm--target-window))
         (buf (and (window-live-p win) (window-buffer win))))
    (if (voxterm--writable-p buf)
        (buffer-name buf)
      (format "%s (fallback; active buffer %s not writable)"
              voxterm-fallback-buffer
              (if (buffer-live-p buf) (buffer-name buf) "none")))))

(defun voxterm--append-fallback (text submit)
  (with-current-buffer (get-buffer-create voxterm-fallback-buffer)
    (goto-char (point-max))
    (unless (bolp) (insert "\n"))
    (insert text)
    (run-hooks 'voxterm-after-insert-hook)
    (if submit (voxterm--submit-here) (insert "\n")))
  (format "%s (fallback)" voxterm-fallback-buffer))

;;;###autoload
(defun voxterm-insert (text &optional submit)
  "Insert TEXT at point in the active buffer, or append to the fallback buffer.
With SUBMIT non-nil, then run RET's binding there — for `claude-repl-mode'
that is `claude-repl-send-input', so dictation actually sends.
Returns a description of where the text went."
  (let* ((win (voxterm--target-window))
         (buf (and (window-live-p win) (window-buffer win))))
    (if (not (voxterm--writable-p buf))
        (voxterm--append-fallback text submit)
      (let ((sent (with-selected-window win
                    (when (and voxterm-space-before
                               (not (bolp))
                               (not (memq (char-before) '(?\s ?\t ?\( ?\[ ?\" ?'))))
                      (insert " "))
                    (insert text)
                    (run-hooks 'voxterm-after-insert-hook)
                    (when submit (voxterm--submit-here)))))
        (if sent
            (format "%s (%s)" (buffer-name buf) sent)
          (buffer-name buf))))))

;;; Speaking the agent's first paragraph ------------------------------------
;;
;; The first paragraph of a streamed reply is the agent's orientation for the
;; turn. Speaking it needs no cooperation from the agent and no surface
;; contract: paragraph breaks are a boundary that already exists. The audio is
;; a pure duplicate of the buffer, so every failure here degrades to silence.

(defcustom voxterm-port 8081
  "Port the voxterm server listens on."
  :type 'integer :group 'voxterm)

(defvar voxterm-speak-stream nil
  "When non-nil, send the first paragraph of each streamed reply to voxterm.
Off by default so a session you are not listening to stays silent.
Toggle with \\[voxterm-toggle-speaking].")

(defvar-local voxterm--stream-acc ""
  "Text streamed so far in the current turn, until a paragraph is sent.")
(defvar-local voxterm--stream-sent nil
  "Non-nil once this turn's paragraph has been queued.")

(defun voxterm--post-say (text)
  "Queue TEXT for speech. Fire-and-forget; never signals."
  (ignore-errors
    (let ((url-request-method "POST")
          (url-request-extra-headers '(("Content-Type" . "application/json")))
          (url-request-data
           (encode-coding-string (json-serialize `(:text ,text)) 'utf-8)))
      (url-retrieve (format "http://127.0.0.1:%d/say" voxterm-port)
                    (lambda (_status) (ignore-errors (kill-buffer (current-buffer))))
                    nil t t))))

(defun voxterm--maybe-send (&optional force)
  "Send the first paragraph of the accumulated stream, if there is one.
With FORCE, send whatever has accumulated — used at end of stream for a
reply that never contained a blank line."
  (unless voxterm--stream-sent
    (let* ((acc voxterm--stream-acc)
           (split (string-match "\n[ \t]*\n" acc))
           (para (cond (split (substring acc 0 split))
                       (force acc)
                       (t nil))))
      (when (and para (string-match-p "[[:alpha:]]" para))
        (setq voxterm--stream-sent t)
        (voxterm--post-say (string-trim para))))))

(defun voxterm--stream-advice (text &rest _)
  "Accumulate streamed TEXT and speak the first paragraph once it closes."
  (when (and voxterm-speak-stream (stringp text) (not voxterm--stream-sent))
    (setq voxterm--stream-acc (concat voxterm--stream-acc text))
    (voxterm--maybe-send)))

(defun voxterm--stream-end-advice (&rest _)
  "Flush a single-paragraph reply, then reset for the next turn."
  (when voxterm-speak-stream (voxterm--maybe-send t))
  (setq voxterm--stream-acc "" voxterm--stream-sent nil))

;;;###autoload
(defun voxterm-toggle-speaking ()
  "Toggle speaking the first paragraph of streamed agent replies."
  (interactive)
  (setq voxterm-speak-stream (not voxterm-speak-stream))
  (if voxterm-speak-stream
      (progn
        (advice-add 'agent-chat-stream-text :before #'voxterm--stream-advice)
        (advice-add 'agent-chat-end-streaming-message :after #'voxterm--stream-end-advice)
        (message "voxterm: speaking agent replies"))
    (advice-remove 'agent-chat-stream-text #'voxterm--stream-advice)
    (advice-remove 'agent-chat-end-streaming-message #'voxterm--stream-end-advice)
    (message "voxterm: silent")))

(provide 'voxterm)
;;; voxterm.el ends here
