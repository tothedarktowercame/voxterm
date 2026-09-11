(require '[clojure.edn :as edn] '[cheshire.core :as json])
(let [s (edn/read-string (slurp (first *command-line-args*)))]
  (when-not (map? s) (throw (ex-info "phase checkpoint is not a map" {})))
  (println (json/generate-string
            {:stage (:stage s)
             :error_code (:error/code s)
             :error_message (:error/message s)
             :retry_attempt (:transport-retry/attempt s)
             :retry_max (:transport-retry/max-attempts s)
             :retry_not_before_ms (:transport-retry/not-before-ms s)
             :repair_kind (:repair/kind s)
             :repair_attempts (:repair/attempts s)
             :repair_max (:repair/max-attempts s)})))
