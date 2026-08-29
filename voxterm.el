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

(defvar voxterm-pinned-buffer-name nil
  "Buffer explicitly selected as voxterm's dictation target.
Nil means continue following terminal focus.")

(defun voxterm--submit-here ()
  "Run whatever RET is bound to in the current buffer; return a label.
Calling the command is deliberate: in `claude-repl-mode' RET is bound to
`claude-repl-send-input', and invoking it directly is more reliable than
synthesising a keypress from a daemon eval."
  (let ((cmd (key-binding (kbd "RET"))))
    (if (commandp cmd)
        (progn (call-interactively cmd) (format "%s" cmd))
      (progn (insert "\n") "newline"))))

(defun voxterm--usable-frame-p (f)
  (and (frame-live-p f)
       (frame-visible-p f)
       (not (frame-parameter f 'voxterm-ignore))))

(defun voxterm--focus-window ()
  "Return the window selected by voxterm's terminal-focus heuristic."
  (let ((frame (or (car (filtered-frame-list
                         (lambda (f) (and (voxterm--usable-frame-p f)
                                          (eq (ignore-errors (frame-focus-state f)) t)))))
                   (and (voxterm--usable-frame-p last-event-frame) last-event-frame)
                   (car (filtered-frame-list #'voxterm--usable-frame-p))
                   (selected-frame))))
    (frame-selected-window frame)))

(defun voxterm--pinned-window ()
  "Return a live window for the pinned buffer, or nil when no pin survives."
  (when voxterm-pinned-buffer-name
    (let ((buf (get-buffer voxterm-pinned-buffer-name)))
      (if (not (buffer-live-p buf))
          (progn (setq voxterm-pinned-buffer-name nil) nil)
        (or (get-buffer-window buf t)
            (let ((win (voxterm--focus-window)))
              (when (window-live-p win)
                (set-window-buffer win buf)
                win)))))))

(defun voxterm--target-window ()
  "The window a dictation should land in: the selected window of the frame
the user is actually typing in.

We are called from a daemon eval, so `selected-frame' is meaningless.  With
several tty frames (`emacsclient -t' per terminal) every one is visible and
the first of `frame-list' is arbitrary — that once sent dictation meant for
claude-13 into claude-4.

Order of preference:
1. a live explicitly pinned buffer;
2. the frame whose terminal reports focus (xterm focus tracking: switching
   terminal windows updates this without a keystroke);
3. `last-event-frame', where the last keystroke happened — the answer when no
   terminal reports focus (emulator without focus events, or all defocused
   because the desktop focus is on a browser);
4. the first visible frame.

A killed pinned buffer clears the pin and resumes this focus order."
  (or (voxterm--pinned-window) (voxterm--focus-window)))

(defun voxterm--writable-p (buf)
  (and (buffer-live-p buf)
       (with-current-buffer buf
         (and (not buffer-read-only)
              (not (minibufferp))))))

;;;###autoload
(defun voxterm-pin (name)
  "Pin voxterm dictation to the live buffer named NAME and raise it."
  (interactive (list (read-buffer "Pin voxterm to buffer: " (buffer-name) t)))
  (let ((buf (get-buffer name)))
    (unless (buffer-live-p buf)
      (user-error "No live buffer named %s" name))
    (setq voxterm-pinned-buffer-name (buffer-name buf))
    (let ((win (voxterm--pinned-window)))
      (unless (window-live-p win)
        (setq voxterm-pinned-buffer-name nil)
        (user-error "Could not display buffer %s" name))
      (select-window win)
      (buffer-name (window-buffer win)))))

;;;###autoload
(defun voxterm-unpin ()
  "Clear the explicit voxterm target and resume following focus."
  (interactive)
  (setq voxterm-pinned-buffer-name nil)
  (voxterm-target-name))

;;;###autoload
(defun voxterm-target-pinned-p ()
  "Return non-nil when voxterm currently has a live explicit target pin."
  (and (voxterm--pinned-window) t))

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

;;; Live preview of the pending buffer ---------------------------------------
;;
;; Always-listening mode buffers segments on the phone until "rocket". Showing
;; that buffer as ghost text at point in the target buffer lets the user see
;; what is about to be sent — including noise residue ("Puck. Pelley.") — from
;; the screen they are already looking at. It is an overlay, so nothing is in
;; the buffer text until `voxterm-insert' replaces it.

(defface voxterm-preview-face
  '((t :inherit shadow :slant italic))
  "Face for dictation buffered on the phone but not yet sent.")

(defvar voxterm--preview-overlay nil)

(defun voxterm--fill (text width)
  "TEXT hard-wrapped to WIDTH columns."
  (with-temp-buffer
    (insert text)
    (let ((fill-column (max 20 width)))
      (fill-region (point-min) (point-max)))
    (buffer-string)))

(defun voxterm--clear-preview ()
  (when (overlayp voxterm--preview-overlay)
    (delete-overlay voxterm--preview-overlay))
  (setq voxterm--preview-overlay nil))

;;;###autoload
(defun voxterm-preview (text)
  "Show TEXT as ghost text at point in the dictation target; empty TEXT clears.
Returns the buffer name, or nil if the target is not writable."
  (voxterm--clear-preview)
  (let* ((win (voxterm--target-window))
         (buf (and (window-live-p win) (window-buffer win))))
    (when (and (voxterm--writable-p buf)
               (not (string-empty-p (string-trim text))))
      (with-current-buffer buf
        (let* ((pos (window-point win))
               ;; Both ends advance: the REPL streams agent output at exactly
               ;; this position, and with front-advance alone the overlay
               ;; stayed behind the stream and scrolled off with the ghost
               ;; text (only a line of it visible, 2026-08-24).
               (ov (make-overlay pos pos buf t t)))
          ;; A before-string, not an after-string: the input line sits at the
          ;; bottom of the window, and redisplay only keeps the cursor row on
          ;; screen — text *after* point spilled below the edge, so only the
          ;; first line of the preview was ever visible. Hard-filled to the
          ;; window width so it lays out the same whatever the wrap settings.
          (overlay-put ov 'before-string
                       (propertize (concat "\N{U+1F399} "
                                           (voxterm--fill (string-trim text)
                                                          (- (window-body-width win) 4))
                                           " \u2026\n")
                                   'face 'voxterm-preview-face))
          (overlay-put ov 'voxterm-preview t)
          (setq voxterm--preview-overlay ov)))
      (buffer-name buf))))

;;;###autoload
(defun voxterm-insert (text &optional submit)
  "Insert TEXT at point in the active buffer, or append to the fallback buffer.
With SUBMIT non-nil, then run RET's binding there — for `claude-repl-mode'
that is `claude-repl-send-input', so dictation actually sends.
Returns a description of where the text went."
  (voxterm--clear-preview)
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
(defvar-local voxterm--stream-start nil
  "When the current turn began streaming, for measuring time-to-first-speech.")

(defun voxterm--post-say (text &optional elapsed-ms kind)
  "Queue TEXT for speech. Fire-and-forget; never signals."
  (ignore-errors
    (let ((url-request-method "POST")
          (url-request-extra-headers '(("Content-Type" . "application/json")))
          (url-request-data
           (encode-coding-string
            (json-serialize `(:text ,text
                              :elapsed_ms ,(or elapsed-ms 0)
                              :kind ,(or kind "paragraph")))
            'utf-8)))
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

(defconst voxterm--gist-re "^[ \t]*Gist:[ \t]*\\(.+?\\)[ \t]*\n"
  "A turn's spoken opening line, per the Gist convention in futon3c/CLAUDE.md.
When an agent follows it there is nothing to infer: this line is the spoken
part, full stop.  Everything below is the fallback for agents that don't.

The trailing newline is load-bearing twice over: it proves the line has
finished streaming, and it stands in for `$', which in an Emacs regexp is an
anchor only at the very end of the pattern — elsewhere it is a literal
dollar sign, so `[ \\t]*$\\n' silently matches nothing.")

(defun voxterm--gist (acc)
  "The Gist line from ACC once it is complete, else nil."
  (when (string-match voxterm--gist-re acc)
    (match-string 1 acc)))

(defun voxterm--collect (acc force)
  "Text to speak from ACC, or nil to keep waiting.
Only paragraphs closed by a blank line count — the trailing one may still
be streaming.  FORCE means the turn has moved on (a tool call started, or
the stream ended), so send whatever is in hand."
  ;; A gist short-circuits everything: no minimum length, no paragraph
  ;; accumulation, no waiting for a tool line to force a flush.
  (or (voxterm--gist acc)
      (voxterm--collect-paragraphs acc force)))

(defun voxterm--collect-paragraphs (acc force)
  "Fallback for turns with no Gist line: accumulate whole paragraphs."
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
        ;; Time from the turn's first streamed token to speech. The open
        ;; question is whether a worker's Gist beats the separate fast model;
        ;; thinking precedes text, so this is the number that decides it.
        (voxterm--post-say
         out
         (and voxterm--stream-start
              (round (* 1000 (float-time (time-since voxterm--stream-start)))))
         (if (voxterm--gist voxterm--stream-acc) "gist" "paragraph"))))))

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
    (unless voxterm--stream-start (setq voxterm--stream-start (current-time)))
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
  (setq voxterm--stream-acc "" voxterm--stream-sent nil
        voxterm--stream-start nil))

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
