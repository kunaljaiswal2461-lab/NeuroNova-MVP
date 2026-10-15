const RETRYABLE = new Set(['model_unavailable', 'llm_unavailable', 'turn_timeout', 'execution_timeout', 'internal_error'])
export const canTryAgain = code => RETRYABLE.has(code)

