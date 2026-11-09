const integerMaximum = 9_000_000_000_000_000;
const creditMaximum = 9_000_000_000_000;

export function parseKeyLimit(value: string, credit = false, maxIps = false): number {
  const text = value.trim();
  if (!text || text.length > 128) throw new Error('invalid_key_limit');
  const number = Number(text);
  if (!Number.isFinite(number) || number < 0 ||
      number > (credit ? creditMaximum : maxIps ? 1_000_000 : integerMaximum) ||
      (!credit && !Number.isSafeInteger(number)) ||
      (credit && number > 0 && (number < 0.000001 ||
        Math.abs(number * 1_000_000 - Math.round(number * 1_000_000)) > 0.00001))) {
    throw new Error('invalid_key_limit');
  }
  return number;
}
