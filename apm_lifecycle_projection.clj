(require '[clojure.edn :as edn] '[clojure.java.io :as io]
         '[cheshire.core :as json])
(defn read-if-present [path]
  (when (.isFile (io/file path)) (edn/read-string (slurp path))))
(let [[registry-path campaign] *command-line-args*
      registry (edn/read-string (slurp registry-path))
      entry (get-in registry [:entries (str "jit-queue:" campaign)])
      state (when entry (edn/read-string (slurp (:coordinator/state-path entry))))
      cdir (when entry (.getParentFile (io/file (:coordinator/state-path entry))))
      queue (when cdir (read-if-present (io/file cdir "queue-state.edn")))
      retry (:coordinator/delayed-retry state)]
  (when-not (and entry (boolean? (:coordinator/enabled? entry)) (map? state))
    (throw (ex-info "coordinator lifecycle unavailable" {})))
  (println (json/generate-string
            {:enabled (:coordinator/enabled? entry)
             :status (:regulator/status state)
             :tick_claim (boolean (:regulator/tick-claim state))
             :queue_observed (boolean queue)
             :active_frame (get-in queue [:active :frame :frame/id])
             :resumption_frames (mapv #(get-in % [:frame :frame/id]) (:resumption-queue queue))
             :queue_status (:status queue)
             :store_read_hold
             (when-let [hold (:store-read/hold queue)]
               {:id (:hold/id hold) :frame (:frame/id hold)
                :problem (:problem/id hold)
                :warning_count (count (:warnings hold))
                :max_elapsed_ms (reduce max 0 (keep :elapsed-ms (:warnings hold)))
                :repair_agent (:repair/agent-id hold) :repair_job (:dispatch/id hold)})
             :retry (when retry {:kind (:kind retry)
                                 :not_before_ms (:not-before-ms retry)
                                 :scheduled_at_ms (:scheduled-at-ms retry)
                                 :attempt (:attempt retry) :max_attempts (:max-attempts retry)
                                 :reason (:error/code (last (:history retry)))})
             :stopped_at (when-not (:coordinator/enabled? entry)
                           (get-in state [:regulator/quiescence-witness :witnessed-at]))
             :last_result (get-in state [:regulator/last-result :status])})))
