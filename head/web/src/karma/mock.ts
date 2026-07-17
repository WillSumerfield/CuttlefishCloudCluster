// Mock domain data for the Karma page.
// Balances are signed integers centred on 0. "You" are Nadia (creditor).

export type Member = {
  id: string;
  name: string;
  // Karma balance, roughly in [-1000, +1000]. 0 = neutral. Public — shown on hover in Standings.
  balance: number;
};

export const you: Member = { id: "u-nadia", name: "Nadia Okafor", balance: 620 };

export const members: Member[] = [
  you,
  { id: "u-01", name: "Ari Solberg",       balance:  880 },
  { id: "u-02", name: "Mira Kwan",         balance:  440 },
  { id: "u-03", name: "Yuki Tashiro",      balance:  180 },
  { id: "u-04", name: "Casimir Bloch",     balance:  -80 },
  { id: "u-05", name: "Junho Park",        balance: -310 },
  { id: "u-06", name: "Sasha Belmonte",    balance: -550 },
  { id: "u-07", name: "Milo Kestrel",      balance: -780 },
];

export const yourStanding = {
  balance: 620,
  escrow: 140,
  escrowSpawns: 2,
};

export type LedgerRow = {
  id: string;
  // + you earned, − you paid, ~ decay
  kind: "earn" | "spend" | "decay";
  amount: number; // absolute value; sign encoded in `kind`
  spawnId?: string;
  arm?: string;
  counterparty?: string;
  when: string; // preformatted, mono
  note: string;
};

export const ledger: LedgerRow[] = [
  { id: "l01", kind: "earn",  amount: 42, spawnId: "spw_9f2a1c", arm: "arm-cephalo-04 · RTX 4090", counterparty: "Junho Park",       when: "14:22 · today",     note: "settled · Junho's train run finished on your GPU" },
  { id: "l02", kind: "spend", amount: 31, spawnId: "spw_9e88a0", arm: "arm-siphon-02 · A6000",     counterparty: "Ari Solberg",       when: "13:58 · today",     note: "settled · finetune-v7 completed" },
  { id: "l03", kind: "decay", amount: 6,                                                                                                when: "12:00 · today",     note: "daily drift toward 0" },
  { id: "l04", kind: "earn",  amount: 88, spawnId: "spw_9d10ff", arm: "arm-cephalo-04 · RTX 4090", counterparty: "Fen Otieno",        when: "22:41 · yesterday", note: "settled · long sweep, 6h04m" },
  { id: "l05", kind: "spend", amount: 17, spawnId: "spw_9cf1b2", arm: "arm-mantle-01 · H100",      counterparty: "Mira Kwan",         when: "19:03 · yesterday", note: "settled · eval batch" },
  { id: "l06", kind: "earn",  amount: 24, spawnId: "spw_9c02aa", arm: "arm-cephalo-04 · RTX 4090", counterparty: "Sasha Belmonte",    when: "16:20 · yesterday", note: "settled" },
  { id: "l07", kind: "spend", amount: 56, spawnId: "spw_9bd4e1", arm: "arm-mantle-01 · H100",      counterparty: "Ari Solberg",       when: "09:12 · yesterday", note: "settled · ablation grid" },
  { id: "l08", kind: "decay", amount: 6,                                                                                                when: "12:00 · yesterday", note: "daily drift toward 0" },
  { id: "l09", kind: "earn",  amount: 19, spawnId: "spw_9a71c8", arm: "arm-cephalo-04 · RTX 4090", counterparty: "Rin Halvorsen",     when: "21:44 · 2d ago",    note: "settled" },
  { id: "l10", kind: "earn",  amount: 33, spawnId: "spw_9a12b0", arm: "arm-cephalo-04 · RTX 4090", counterparty: "Léo Marchand",      when: "15:07 · 2d ago",    note: "settled" },
  { id: "l11", kind: "spend", amount: 21, spawnId: "spw_99e440", arm: "arm-siphon-02 · A6000",     counterparty: "Halide Aksoy",      when: "11:31 · 2d ago",    note: "settled · quick probe" },
  { id: "l12", kind: "decay", amount: 6,                                                                                                when: "12:00 · 2d ago",    note: "daily drift toward 0" },
  { id: "l13", kind: "earn",  amount: 61, spawnId: "spw_98a221", arm: "arm-cephalo-04 · RTX 4090", counterparty: "Casimir Bloch",     when: "23:58 · 3d ago",    note: "settled · overnight" },
  { id: "l14", kind: "spend", amount: 44, spawnId: "spw_9871af", arm: "arm-mantle-01 · H100",      counterparty: "Theo Vance",        when: "17:12 · 3d ago",    note: "settled" },
];

export const escrowSpawns = [
  { id: "spw_a0f331", note: "sweep-λ · arm-mantle-01 · H100",  amount: 90 },
  { id: "spw_a0f118", note: "eval-run  · arm-siphon-02 · A6000", amount: 50 },
];
