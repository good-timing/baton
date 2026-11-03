## python: 0.9.4 published vs 0.9.5 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. Median (lowest–highest) of 5, 5 runs.

| measure | metric | 0.9.4 published | 0.9.5 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.53 (0.528–0.555) | 0.531 (0.525–0.554) | +0% (noise) |
| overhead | plain_p99_ms | 0.621 (0.597–0.667) | 0.616 (0.593–0.661) | -1% (noise) |
| overhead | baton_p50_ms | 0.994 (0.968–1.09) | 1.01 (0.97–1.1) | +1% (noise) |
| overhead | baton_p99_ms | 1.59 (1.54–1.65) | 1.58 (1.53–1.65) | -0% (noise) |
| overhead | added_p50_ms | 0.459 (0.439–0.558) | 0.476 (0.433–0.547) | +4% (noise) |
| overhead | added_p99_ms | 0.944 (0.929–1.03) | 0.975 (0.91–1.06) | +3% (noise) |
| burst | wall_ms | 9,694 (9,592–9,738) | 9,627 (9,560–9,785) | -1% (noise) |
| burst | call_p50_ms | 9.53 (9.41–9.56) | 9.43 (9.36–9.62) | -1% (noise) |
| burst | call_p99_ms | 13.9 (13.9–14.1) | 14 (13.9–14.1) | +0% (noise) |
| burst | close_ms | 1.46 (1.31–1.91) | 1.49 (1.42–1.91) | +2% (noise) |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 1,642 (1,623–1,659) | 1,643 (1,635–1,714) | +0% (noise) |
| outage_hang | call_p50_ms | 8.02 (7.94–8.09) | 8.03 (7.99–8.36) | +0% (noise) |
| outage_hang | call_p99_ms | 12.1 (11.9–13) | 12.6 (11.7–12.7) | +4% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | -0% |
| outage_hang | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | wall_ms | 1,706 (1,703–1,734) | 1,706 (1,698–1,761) | +0% (noise) |
| outage_down | call_p50_ms | 8.39 (8.37–8.49) | 8.39 (8.3–8.64) | -0% (noise) |
| outage_down | call_p99_ms | 12.6 (12.3–13.1) | 12.8 (12.5–13) | +1% (noise) |
| outage_down | close_ms | 706 (706–706) | 706 (706–706) | +0% (noise) |
| outage_down | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| memory | heap_after_warmup_bytes | 611,558 (595,469–619,843) | 602,789 (596,414–610,862) | -1% (noise) |
| memory | heap_at_half_bytes | 616,231 (608,838–624,469) | 617,388 (610,311–630,911) | +0% (noise) |
| memory | heap_at_end_bytes | 618,913 (613,861–633,500) | 629,132 (624,332–631,564) | +2% (noise) |
| memory | growth_per_call_bytes | 0.582 (-0.832–1.44) | 0.961 (0.0185–1.43) | +0.379 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,494,785 (1,492,689–1,496,844) | 1,493,946 (1,484,296–1,495,964) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,615,181 (3,614,290–3,624,198) | 3,618,591 (3,618,166–3,624,437) | +0% (noise) |
| memory_outage | heap_at_end_bytes | 3,535,558 (3,531,867–3,541,671) | 3,532,337 (3,531,222–3,536,947) | -0% (noise) |
| memory_outage | growth_per_call_bytes | -8.22 (-8.29–-7.87) | -8.72 (-9.05–-8.4) | -6% |
| process | max_rss_bytes | 88,358,912 (88,272,896–93,036,544) | 88,309,760 (87,986,176–92,385,280) | -0% (noise) |
