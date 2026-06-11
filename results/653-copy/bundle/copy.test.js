const { verifyNumbers, buildThread } = require('../src/copy');

// SPEC authored from the operator's picks. $0.32 is the discriminator: its
// digit-runs are 0 AND 32, so a correct allowed-number set must contain both.
const SPEC = {
  repoName: 'launch-content-pipeline',
  headline: 'We shipped.',
  metrics: [
    { name: 'cost', value: '$0.32', scope: 'sonnet 1-shot, cheapest arm' },
    { name: 'samples', value: '27', scope: 'n=27, dayjs/jest, 1 rep' },
  ],
  scopeGuard: 'all numbers measured locally',
  cta: 'Star the repo.',
};

describe('verifyNumbers — number safety', () => {
  test('ACCEPT: in-spec values with a scope guard pass, incl. multi-digit-run $0.32 (runs 0 AND 32)', () => {
    const res = verifyNumbers('$0.32 (n=27, dayjs/jest, 1 rep)', SPEC);
    expect(res.ok).toBe(true);
    expect(res.violations).toEqual([]);
  });

  test('REJECT: a number absent from the spec is fabricated', () => {
    const res = verifyNumbers('99 win rate (n=27, dayjs/jest, 1 rep)', SPEC);
    expect(res.ok).toBe(false);
    expect(res.violations).toContainEqual({ type: 'fabricated', value: '99' });
  });

  test('REJECT: an in-spec value stated without its scope guard is unguarded', () => {
    const res = verifyNumbers('$0.32', SPEC);
    expect(res.ok).toBe(false);
    expect(res.violations.some((v) => v.type === 'unguarded')).toBe(true);
  });
});

describe('buildThread', () => {
  test('returns [hook, one tweet per metric (value+scope inline), cta]', () => {
    const thread = buildThread({ spec: SPEC, generateHook: (s) => s.headline });
    expect(thread).toHaveLength(SPEC.metrics.length + 2);
    expect(thread[0]).toBe('We shipped.');
    expect(thread[1]).toContain('$0.32');
    expect(thread[1]).toContain('sonnet 1-shot, cheapest arm');
    expect(thread[thread.length - 1]).toBe('Star the repo.');
  });

  test('throws when the hook smuggles a fabricated number', () => {
    expect(() =>
      buildThread({ spec: SPEC, generateHook: () => '99 win rate' })
    ).toThrow();
  });
});
