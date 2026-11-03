## python: 0.9.3 published vs 0.9.4 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. Median (lowest–highest) of 5, 5 runs.

| measure | metric | 0.9.3 published | 0.9.4 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.225 (0.218–0.235) | 0.22 (0.213–0.234) | -2% (noise) |
| overhead | plain_p99_ms | 0.265 (0.263–0.378) | 0.266 (0.24–0.336) | +1% (noise) |
| overhead | baton_p50_ms | 1.37 (1.32–1.42) | 0.428 (0.419–0.507) | -69% |
| overhead | baton_p99_ms | 2.32 (1.9–2.83) | 0.812 (0.739–0.965) | -65% |
| overhead | added_p50_ms | 1.14 (1.1–1.2) | 0.21 (0.186–0.279) | -82% |
| overhead | added_p99_ms | 1.98 (1.52–2.57) | 0.545 (0.473–0.654) | -72% |
| burst | wall_ms | 5,826 (5,498–6,295) | 4,440 (4,424–4,683) | -24% |
| burst | call_p50_ms | 5.52 (5.38–6.11) | 4.26 (4.21–4.51) | -23% |
| burst | call_p99_ms | 8.97 (7.67–12.1) | 7.49 (7.15–8.21) | -17% |
| burst | close_ms | 976 (958–1,049) | 1.36 (0.958–1.89) | -100% |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 2,309 (2,269–2,312) | 20,000 (20,000–20,000) | +766% |
| burst | lost | 17,691 (17,688–17,731) | 0 (0–0) | -100% |
| outage_hang | wall_ms | 725 (702–772) | 796 (765–820) | +10% |
| outage_hang | call_p50_ms | 3.45 (3.39–3.58) | 3.62 (3.5–3.75) | +5% |
| outage_hang | call_p99_ms | 6.35 (6.04–9.76) | 7.33 (7.07–7.61) | +15% (noise) |
| outage_hang | close_ms | 3,001 (3,001–3,003) | 3,005 (3,005–3,005) | +0% |
| outage_hang | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | wall_ms | 791 (749–814) | 769 (721–777) | -3% (noise) |
| outage_down | call_p50_ms | 3.45 (3.43–3.57) | 3.72 (3.48–3.74) | +8% |
| outage_down | call_p99_ms | 6.98 (6.13–11.6) | 6.61 (6.14–6.7) | -5% (noise) |
| outage_down | close_ms | 1,329 (1,308–1,409) | 705 (7.45–705) | -47% |
| outage_down | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| memory | heap_after_warmup_bytes | 759,167 (756,981–767,202) | 598,118 (595,323–606,100) | -21% |
| memory | heap_at_half_bytes | 3,118,851 (3,116,993–3,119,084) | 620,174 (617,698–630,408) | -80% |
| memory | heap_at_end_bytes | 3,119,322 (3,117,407–3,119,443) | 619,760 (618,397–641,910) | -80% |
| memory | growth_per_call_bytes | 0.0359 (0.0307–0.0471) | -0.0342 (-0.943–2.17) | -195% |
| memory_outage | heap_after_warmup_bytes | 1,326,516 (1,324,935–1,328,351) | 1,495,603 (1,494,119–1,496,840) | +13% |
| memory_outage | heap_at_half_bytes | 3,104,055 (3,083,236–3,105,072) | 3,622,784 (3,614,650–3,628,017) | +17% |
| memory_outage | heap_at_end_bytes | 3,103,701 (3,079,952–3,106,214) | 3,616,241 (3,536,935–3,629,953) | +17% |
| memory_outage | growth_per_call_bytes | 0.173 (-2.45–2.05) | 0.159 (-8.68–1.33) | -0.0138 (noise) |
| process | max_rss_bytes | 93,302,784 (92,553,216–95,961,088) | 87,298,048 (87,056,384–92,000,256) | -6% |
