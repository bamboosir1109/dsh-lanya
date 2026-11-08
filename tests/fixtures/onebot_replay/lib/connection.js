// Synthetic transport contract: a real network endpoint is never constructed.
export class OneBotConnection {
    constructor(config) { this.config = config; }
    start() { throw new Error('Synthetic transport cannot start'); }
}
