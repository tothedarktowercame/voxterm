(require '[clojure.edn :as edn] '[cheshire.core :as json])
(let [[registry-path campaign] *command-line-args*
      registry (edn/read-string (slurp registry-path))
      entry (get-in registry [:entries (str "jit-queue:" campaign)])
      state (when entry (edn/read-string (slurp (:coordinator/state-path entry))))]
  (when-not (and entry (boolean? (:coordinator/enabled? entry)) (map? state))
    (throw (ex-info "coordinator lifecycle unavailable" {})))
  (println (json/generate-string
            {:enabled (:coordinator/enabled? entry)
             :status (:regulator/status state)
             :tick_claim (boolean (:regulator/tick-claim state))
             :stopped_at (get-in state [:regulator/quiescence-witness :witnessed-at])
             :last_result (get-in state [:regulator/last-result :status])})))
