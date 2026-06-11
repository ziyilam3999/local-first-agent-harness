function collectAllowedNumbers(spec) {
  const allowed = new Set();
  const fields = [spec.repoName, spec.headline, spec.scopeGuard, spec.cta];
  for (const m of spec.metrics) {
    fields.push(m.name, m.value, m.scope);
  }
  for (const field of fields) {
    if (!field) continue;
    const runs = String(field).match(/\d+/g) || [];
    for (const run of runs) {
      allowed.add(run);
    }
  }
  return allowed;
}

function verifyNumbers(text, spec) {
  const allowed = collectAllowedNumbers(spec);
  const violations = [];

  const runs = String(text).match(/\d+/g) || [];
  for (const run of runs) {
    if (!allowed.has(run)) {
      violations.push({ type: 'fabricated', value: run });
    }
  }

  const scopes = spec.metrics.map((m) => m.scope);
  if (spec.scopeGuard) scopes.push(spec.scopeGuard);
  const guardPresent = scopes.some((s) => s && text.includes(s));

  for (const m of spec.metrics) {
    if (text.includes(m.value) && !guardPresent) {
      violations.push({ type: 'unguarded', metric: m.name, value: m.value });
    }
  }

  return { ok: violations.length === 0, violations };
}

function buildThread({ spec, generateHook }) {
  const hook = generateHook(spec);
  const check = verifyNumbers(hook, spec);
  if (!check.ok) {
    throw new Error('hook violates number-safety: ' + JSON.stringify(check.violations));
  }
  const tweets = spec.metrics.map((m) => `${m.value} — ${m.scope}`);
  return [hook, ...tweets, spec.cta];
}

module.exports = { verifyNumbers, buildThread };
