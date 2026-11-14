export function trigger(): never {
  throw new Error("indexed failure");
}
export function start(): void {
  trigger();
}
