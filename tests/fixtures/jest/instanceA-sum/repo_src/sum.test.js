const { sum } = require('./sum');

test('sum adds two positive numbers', () => {
  expect(sum(1, 2)).toBe(3);
});

test('sum of zeros is zero', () => {
  expect(sum(0, 0)).toBe(0);
});
