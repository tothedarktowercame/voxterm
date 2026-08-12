;;; voxterm.el --- receive dictated text from the voxterm server -*- lexical-binding: t; -*-

;; Loaded on demand by voxterm's server.py via emacsclient; nothing to add to init.el.
;; Text arrives as a plain elisp call, so Emacs needs no listener of its own.

;;; Code:

(require 'cl-lib)

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

(defcustom voxterm-min-chars 120
  "Keep adding paragraphs until the spoken text reaches this many characters.
One turn in four opens with a paragraph under 100 characters — about six
seconds of speech — too thin to be worth hearing on its own.  Measured
over recent transcripts the median opener is 145 characters, so 120 pads
the thin quarter while leaving a typical turn to send on its own; raising
this much above the median makes ordinary turns wait for a second
paragraph that may never come.

Waiting is cheap either way: a tool line or the end of the stream flushes
whatever has accumulated."
  :type 'integer :group 'voxterm)

(defcustom voxterm-max-paragraphs 3
  "Never speak more than this many paragraphs, however short they are."
  :type 'integer :group 'voxterm)

(defun voxterm--collect (acc force)
  "Text to speak from ACC, or nil to keep waiting.
Only paragraphs closed by a blank line count — the trailing one may still
be streaming.  FORCE means the turn has moved on (a tool call started, or
the stream ended), so send whatever is in hand."
  (let* ((ends-blank (string-match-p "\n[ \t]*\n[ \t]*\\'" acc))
         (paras (split-string acc "\n[ \t]*\n" t "[ \t\n]+"))
         (complete (if (or ends-blank force) paras (butlast paras)))
         (out "") (n 0) (enough nil))
    (catch 'done
      (dolist (p complete)
        (setq out (string-trim (concat out " " p))
              n (1+ n))
        (when (or (>= (length out) voxterm-min-chars)
                  (>= n voxterm-max-paragraphs))
          (setq enough t)
          (throw 'done nil))))
    (when (and (not (string-empty-p out))
               (or force enough)
               (string-match-p "[[:alpha:]]" out))
      out)))

(defun voxterm--flush (&optional force)
  "Send accumulated prose if it is ready, or if FORCE."
  (unless voxterm--stream-sent
    (let ((out (voxterm--collect voxterm--stream-acc force)))
      (when out
        (setq voxterm--stream-sent t)
        (voxterm--post-say out)))))

(defun voxterm--tool-line-p (text)
  "Non-nil if TEXT is claude-repl tool progress rather than agent prose.
Tool events arrive through the same `agent-chat-stream-text' call
\(claude-repl.el:1044\) and carry no distinguishing face — the tool styling
is applied afterwards as an overlay — so they can only be recognised by
shape.  Every line is \"[Name] preview\", per
`claude-repl--format-tool-detail'."
  (let ((lines (delq nil (mapcar (lambda (l)
                                   (let ((s (string-trim l)))
                                     (unless (string-empty-p s) s)))
                                 (split-string text "\n")))))
    (and lines
         (not (cl-find-if-not (lambda (l) (string-prefix-p "[" l)) lines)))))

(defun voxterm--stream-advice (text &rest _)
  "Accumulate streamed TEXT and speak once enough prose has arrived."
  (when (and voxterm-speak-stream (stringp text) (not voxterm--stream-sent))
    (if (voxterm--tool-line-p text)
        ;; A tool line means the agent has stopped writing prose and started
        ;; working. Say what we have rather than waiting for a paragraph that
        ;; may never come.
        (voxterm--flush t)
      (setq voxterm--stream-acc (concat voxterm--stream-acc text))
      (voxterm--flush))))

(defun voxterm--stream-end-advice (&rest _)
  "Flush whatever is left, then reset for the next turn."
  (when voxterm-speak-stream (voxterm--flush t))
  (setq voxterm--stream-acc "" voxterm--stream-sent nil))

;;;###autoload
(defun voxterm-context (&optional chars)
  "Return the last CHARS of the conversation buffer, base64 encoded.
Base64 because the text comes back through `emacsclient -e', where a
multi-line propertised string is painful to parse reliably.  Text
properties are dropped; the caller only wants the words."
  (let* ((chars (or chars 6000))
         (win (voxterm--target-window))
         (buf (and (window-live-p win) (window-buffer win))))
    (if (not (buffer-live-p buf))
        ""
      (with-current-buffer buf
        (base64-encode-string
         (encode-coding-string
          (buffer-substring-no-properties (max (point-min) (- (point-max) chars))
                                          (point-max))
          'utf-8)
         t)))))

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
