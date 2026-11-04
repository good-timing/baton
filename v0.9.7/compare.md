## python: 0.9.6 published vs 0.9.7 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. Median (lowest–highest) of 5, 5 runs.

| measure | metric | 0.9.6 published | 0.9.7 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.542 (0.514–0.583) | 0.532 (0.526–0.536) | -2% (noise) |
| overhead | plain_p99_ms | 0.639 (0.569–0.728) | 0.62 (0.592–0.638) | -3% (noise) |
| overhead | baton_p50_ms | 0.994 (0.967–1.01) | 0.988 (0.981–1) | -1% (noise) |
| overhead | baton_p99_ms | 1.67 (1.53–1.71) | 1.65 (1.61–1.71) | -1% (noise) |
| overhead | added_p50_ms | 0.444 (0.427–0.466) | 0.452 (0.447–0.471) | +2% (noise) |
| overhead | added_p99_ms | 1.02 (0.962–1.07) | 1.03 (0.99–1.07) | +0% (noise) |
| burst | wall_ms | 9,889 (9,520–10,154) | 9,807 (9,721–9,920) | -1% (noise) |
| burst | call_p50_ms | 9.68 (9.36–9.93) | 9.67 (9.55–9.73) | -0% (noise) |
| burst | call_p99_ms | 14.1 (13.5–14.7) | 13.8 (13.7–14.1) | -2% (noise) |
| burst | close_ms | 1.78 (1.57–1.87) | 1.9 (1.54–2.3) | +7% |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 1,658 (1,610–1,694) | 1,645 (1,643–1,669) | -1% (noise) |
| outage_hang | call_p50_ms | 8.11 (7.92–8.24) | 8.09 (8.05–8.16) | -0% (noise) |
| outage_hang | call_p99_ms | 12.2 (11.7–12.6) | 12.1 (11.8–12.7) | -1% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,006) | 3,005 (3,005–3,005) | -0% (noise) |
| outage_hang | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | wall_ms | 1,752 (1,699–1,801) | 1,728 (1,707–1,749) | -1% (noise) |
| outage_down | call_p50_ms | 8.62 (8.34–8.89) | 8.49 (8.39–8.61) | -2% (noise) |
| outage_down | call_p99_ms | 12.9 (12.4–13) | 12.8 (12.3–13.6) | -1% (noise) |
| outage_down | close_ms | 706 (706–707) | 706 (706–707) | -0% |
| outage_down | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| memory | heap_after_warmup_bytes | 609,028 (604,335–617,332) | 607,396 (527,270–616,941) | -0% (noise) |
| memory | heap_at_half_bytes | 629,401 (621,454–634,958) | 625,590 (623,212–632,532) | -1% (noise) |
| memory | heap_at_end_bytes | 640,759 (629,029–647,801) | 624,969 (623,084–635,818) | -2% |
| memory | growth_per_call_bytes | 1.02 (0.118–2.21) | 0.0796 (-0.756–1.02) | -92% |
| memory_outage | heap_after_warmup_bytes | 1,503,350 (1,501,660–1,504,376) | 1,504,171 (1,501,454–1,505,365) | +0% (noise) |
| memory_outage | heap_at_half_bytes | 3,616,047 (3,611,737–3,619,424) | 3,619,487 (3,619,020–3,619,624) | +0% |
| memory_outage | heap_at_end_bytes | 3,542,506 (3,538,914–3,543,750) | 3,542,425 (3,541,270–3,544,029) | -0% (noise) |
| memory_outage | growth_per_call_bytes | -7.28 (-7.73–-7.23) | -7.69 (-7.81–-7.55) | -6% (noise) |
| large_result | plain_p50_ms | 0.483 (0.466–0.5) | 0.469 (0.456–0.499) | -3% (noise) |
| large_result | plain_p99_ms | 0.546 (0.518–0.567) | 0.536 (0.534–0.564) | -2% (noise) |
| large_result | baton_p50_ms | 1.01 (0.995–1.04) | 1.02 (0.998–1.03) | +1% (noise) |
| large_result | baton_p99_ms | 2.87 (2.84–3.03) | 2.96 (2.73–3.04) | +3% (noise) |
| large_result | added_p50_ms | 0.525 (0.505–0.566) | 0.556 (0.529–0.561) | +6% (noise) |
| large_result | added_p99_ms | 2.31 (2.27–2.5) | 2.43 (2.2–2.48) | +5% (noise) |
| large_result | heap_growth_bytes | 2,158,178 (2,157,515–2,158,837) | 2,156,018 (2,147,099–2,159,042) | -0% |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 89,010,176 (88,780,800–94,216,192) | 89,128,960 (88,977,408–93,442,048) | +0% (noise) |
