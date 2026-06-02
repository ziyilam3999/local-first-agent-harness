const { capitalize } = require('./str');

test('capitalize upper-cases the first letter', () => {
  expect(capitalize('hello')).toBe('Hello');
});

test('capitalize leaves an empty string empty', () => {
  expect(capitalize('')).toBe('');
});
