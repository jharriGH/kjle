# KJLE Niche Slug Canonical Map
**Generated:** 2026-09-22  
**Status:** PENDING EXECUTION (bulk UPDATEs deferred — DB IO waiters=6 at time of analysis)  
**Inventory:** 5,688 distinct slugs across 1,226,266 leads (84,641 null/blank)

---

## How to read this document

| Column | Meaning |
|--------|---------|
| **Canonical** | The one slug consumers and campaign prep should use |
| **Merge FROM** | Safe to retag to canonical — same business type, confirmed clean |
| **LEAKY** | Broad/contaminated bucket — do NOT merge into a clean slug; see Leaky Bucket section |
| **Separate** | Related but distinct niche — keep its own slug |

---

## LEAKY BUCKETS — DO NOT MERGE INTO CLEAN SLUGS

These slugs contain off-niche businesses confirmed by category sampling. Merging them into clean canonical slugs would contaminate the clean set. Approach: point consumers to the clean canonical slug; reclassify leaky rows separately (needs Jim sign-off).

| Slug | Count | Confirmed leakage |
|------|-------|------------------|
| `roofing` | 16,093 | Windows Installation (260), Drywall (109), Insulation (214), Countertop install (150), Supply Shop (81) |
| `hvac` | 21,808 | Acupuncture clinic (131), Alternative medicine (128), Business mfg & supply (161) |
| `plumbing` | 12,863 | Lymph drainage therapist (14), Pipe supplier (76), Plumbers merchant (300) |
| `landscaping` | 3,636 | Gutter Services (193), Pressure Washers (171), Tree Services (90) |
| `cleaning` | 1,843 | Hair Removal (91), Drywall services (82) |
| `electrical` | 1,691 | Electrical supply store/shop (88), Electric Utility Company (28), Electricity supplier (26) |
| `concrete` | 8,195 | Ready-Mix Concrete Supplier (274), Concrete product supplier (85), Cement mfg/supplier (24) |
| `painting` | 2,244 | Mostly painters but includes Paint shop (7), Commercial printer (4) — borderline |
| `contractor` | 4,813 | All-null categories; origin data source unknown — treat as unverified |
| `contractors` | 4,290 | All-null categories; same issue as `contractor` |
| `other` | 24,578 | Generic catch-all — do not consume |
| `retail` | 31,069 | Generic catch-all |
| `home_services` | 7,542 | Generic catch-all |
| `local_services` | 1,662 | Generic catch-all |
| `professional_services` | 1,705 | Generic catch-all |
| `pest_control` | 884 | Broad term; `pest_control_service` is the clean slug |
| `medical` | 3,081 | Extremely broad — contains all medical categories |
| `realestate` | 34,480 | Clean (all real estate) but merging requires Jim sign-off — large operation (34k rows) |
| `real_estate` | 2,780 | Needs sampling — may include commercial, land, etc. |
| `general_contractor` | 40,622 | Relatively clean but very broad catch-all; contains Home builders, Remodellers, Kitchen Renovators |
| `dental` | 16,119 | Appears clean (all dental specialties) but is a broad parent; specialists have own slugs |
| `yoga` | 690 | Needs verification — may be leaky |
| `landscaper` | 1,554 | Overlaps landscaping (leaky bucket); needs verification |

---

## CANONICAL MAP — SAFE CONSOLIDATIONS

All merges below are DEFERRED until IO waiters < 2. Execute in batches of ≤ 20k rows.

### GROUP 1: HTML-Entity Encoding Bugs (highest priority — same slug, just URL-encoded)
These are 100% safe: same logical slug stored with `&amp;` instead of `&`.

| FROM (encoded) | Count | TO (canonical) | Canonical Count |
|----------------|-------|----------------|-----------------|
| `heating_&amp;_air_conditioning/hvac` | 300 | `heating_&_air_conditioning/hvac` | 2,245 |
| `health_&amp;_medical` | 246 | `health_&_medical` | 1,478 |
| `fitness_&amp;_instruction` | 75 | `fitness_&_instruction` | 1,446 |
| `child_care_&amp;_day_care` | 160 | `child_care_&_day_care` | 166 |
| `party_&amp;_event_planning` | 40 | `party_&_event_planning` | 951 |
| `beauty_&amp;_spa` | 272 | `beauty_&_spas` | 633 |
| `eyewear_&amp;_optician` | 39 | `eyewear_&_opticians` | 136 |
| `bank_&amp;_credit_union` | 53 | `banks_&_credit_unions` | 178 |
| `counseling_&amp;_mental_health` | 34 | `counseling_&_mental_health` | 217 |
| `auto_part_&amp;_supply` | 40 | `auto_parts_&_supplies` | 176 |
| `event_planning_&amp;_service` | 44 | `event_planning_&_services` | 2,451 |
| `art_&amp;_entertainment` | 69 | `arts_&_entertainment` | 203 |
| `venue_&amp;_event_space` | 185 | `venues_&_event_spaces` | 603 |
| `fence_&amp;_gate` | 42 | `fences_&_gates` | 255 |

**Group 1 total rows to retag: ~1,599**

---

### GROUP 2: Simple Plurals → Canonical Singular (or largest clean form)

| FROM | Count | TO (canonical) | Canonical Count |
|------|-------|----------------|-----------------|
| `dentists` | 1,001 | `dentist` | 11,625 |
| `electricians` | 1,068 | `electrician` | 6,216 |
| `architects` | 558 | `architect` | 6,006 |
| `chiropractors` | 554 | `chiropractor` | 8,636 |
| `orthodontists` | 148 | `orthodontist` | 2,491 |
| `optometrists` | 128 | `optometrist` | 3,295 |
| `lawyers` | 673 | `lawyer` | 5,325 |
| `photographers` | 2,124 | `photographer` | 9,773 |
| `florists` | 136 | `florist` | 4,357 |
| `veterinarians` | 221 | `veterinarian` | 3,994 |
| `nutritionists` | 408 | `nutritionist` | 2,218 |
| `dermatologists` | 56 | `dermatologist` | 546 |
| `hair_salons` | 214 | `hair_salon` | 775 |
| `tree_services` | 1,054 | `tree_service` | 4,681 |
| `landscape_architects` | 340 | `landscape_architect` | 1,116 |
| `structural_engineers` | 112 | `structural_engineer` | 190 |
| `metal_fabricators` | 130 | `metal_fabricator` | 323 |
| `mortgage_brokers` | 517 | `mortgage_broker` | 1,412 |
| `mortgage_lenders` | 132 | `mortgage_lender` | 2,669 |
| `pet_groomers` | 144 | `pet_groomer` | 2,350 |
| `used_car_dealers` | 90 | `used_car_dealer` | 4,192 |
| `car_dealers` | 155 | `car_dealer` | 2,172 |
| `car_dealership` | 96 | `car_dealer` | 2,172 |
| `medical_spas` | 693 | `medical_spa` | 11,430 |
| `doctors` | 977 | `doctor` | 752 |
| `caterers` | 421 | `caterer` | 4,006 |
| `djs` | 307 | `dj` | 635 |
| `chimney_sweeps` | 99 | `chimney_sweep` | 189 |
| `painters` | 750 | `painter_and_decorator` | 923 |
| `painter` | 42 | `painter_and_decorator` | 923 |

**Group 2 total rows to retag: ~11,448**

---

### GROUP 3: Close Synonyms (same service, confirmed-clean pairs)

| FROM | Count | TO (canonical) | Canonical Count | Basis |
|------|-------|----------------|-----------------|-------|
| `roofing_contractor` | 187 | `roofing_service` | 9,981 | Both confirmed clean roofing |
| `dental_clinic` | 1,846 | `dentist` | 11,625 | All dental per sampling |
| `tree_services` | (see Group 2) | — | — | — |
| `veterinary` | 66 | `veterinarian` | 3,994 | Same business type |
| `veterinary_care` | 213 | `veterinarian` | 3,994 | Same business type |
| `lawn_services` | 172 | `lawn_care_service` | 3,585 | Same service |
| `demolition_services` | 264 | `demolition_contractor` | 757 | Same service |
| `excavation_services` | 105 | `excavating_contractor` | 847 | Same service |
| `air_duct_cleaning` | 398 | `air_duct_cleaning_service` | 924 | Same service |
| `carpet_cleaning` | 272 | `carpet_cleaning_service` | 1,242 | Same service |
| `dumpster_rental` | 63 | `dumpster_rental_service` | 84 | Same service |
| `septic_services` | 80 | `septic_tank_service` | 138 | Same service |
| `insulation_installation` | 149 | `insulation_contractor` | 341 | Same service |
| `wedding_planning` | 760 | `wedding_planner` | 1,388 | Same service |
| `mover` | 74 | `movers` | 658 | Same service |
| `removals_service` | 838 | `removals_company` | 2,366 | Same service |
| `snow_removal` | 270 | `snow_removal_service` | 151 | More precise form |
| `pressure_washers` | 390 | `pressure_washing_service` | 1,031 | Same service |
| `pool_cleaners` | 67 | `pool_cleaning_service` | 449 | Same service |
| `garage_door_service` | 61 | `garage_door_services` | 429 | Same service |
| `acupuncturist` | 292 | `acupuncture_clinic` | 417 | Same service |
| `acupuncture` | 291 | `acupuncture_clinic` | 417 | Same service |
| `limos` | 226 | `limousine_service` | 1,829 | Same service |
| `limo` | 171 | `limousine_service` | 1,829 | Same service |
| `limo_service` | 65 | `limousine_service` | 1,829 | Same service |
| `personal_injury_attorney` | 83 | `personal_injury_lawyer` | 5,221 | Same service |
| `personal_injury_law` | 167 | `personal_injury_lawyer` | 5,221 | Same service |
| `bankruptcy_law` | 65 | `bankruptcy_lawyer` | 624 | Same service |
| `real_estate_agent` | 2,260 | `estate_agent` | 38,183 | Same business type |
| `real_estate_agents` | 4,964 | `estate_agent` | 38,183 | Same business type |
| `estate_agents` | 135 | `estate_agent` | 38,183 | Same business type |

**Group 3 total rows to retag: ~11,758**

---

### GRAND TOTAL (deferred): ~24,805 rows across all groups

---

## LEAKY BUCKET RECOMMENDATIONS (awaiting Jim sign-off)

### 1. `roofing` (16,093) — recommended approach
- **Clean rows:** ~12,055 (Roofing Service + Roofing categories only)
- **Off-niche rows:** ~4,038 (windows, waterproofing, drywall, insulation, countertops)
- **Recommendation:** Filter by `category IN ('Roofing Service', 'Roofing')` → retag to `roofing_service`. Retag remaining to their correct slug (windows_installation, waterproofing, drywall_installation, insulation_contractor). Do NOT bulk-merge entire `roofing` bucket.

### 2. `hvac` (21,808) — recommended approach
- **Clean rows:** ~18,084 (HVAC contractor, Heating & AC/HVAC, HVAC services, Air conditioning contractor, Central Heating Service, Air conditioning repair)
- **Off-niche rows:** ~3,724 (acupuncture, alt medicine, business mfg supply — likely misclassification at scrape time)
- **Recommendation:** Filter by HVAC-category keywords → retag to `hvac_contractor`. Off-niche rows need reclassification by category.

### 3. `plumbing` (12,863) — recommended approach
- **Clean rows:** ~11,000 (Plumbing, Plumber, Plumbers, Plumbing Service)
- **Off-niche rows:** ~863 (Lymph drainage therapist, Pipe supplier, Plumbers merchant, Drainage service)
- **Recommendation:** Filter by plumbing category → retag to `plumber`. Plumbers merchant → `plumbers_merchant`. Lymph drainage → reclassify.

### 4. `landscaping` (3,636) — recommended approach
- **Clean rows:** ~2,222 (Landscaping, Landscape Gardener, Landscape designer)
- **Off-niche rows:** ~1,414 (Gutter Services, Pressure Washers, Tree Services, Artificial Turf)
- **Recommendation:** Retag clean rows to `landscape_gardener`. Retag gutter → `gutter_cleaning_service`, pressure washers → `pressure_washing_service`, tree → `tree_service`.

### 5. `cleaning` (1,843) — recommended approach
- **Off-niche rows:** Hair Removal (91), Drywall services (82)
- **Recommendation:** These are very small contaminations. Reclassify the ~173 off-niche rows by category, then the bulk becomes safe to point consumers to.

### 6. `contractor` / `contractors` (4,813 + 4,290 = 9,103 combined) — recommended approach
- **Problem:** All rows have NULL category — impossible to verify what businesses these are without name inspection.
- **Recommendation:** Sample 100 `business_name` rows from each and manually review. If they're verified as legitimate general contractors, merge to `general_contractor`. Otherwise isolate.

### 7. `realestate` (34,480) — recommended approach (requires Jim sign-off)
- Category sampling shows 100% real estate businesses (real estate agents, estate agents, real estate consultants).
- **Recommendation:** Safe to merge to `estate_agent` (38,183) — but it's a 34k-row UPDATE. Needs Jim explicit go before executing.

---

## SAFE EXECUTION SCRIPT (off-peak, IO waiters < 2)

Run in this order, ≤ 20k rows per statement, with load checks between groups:

```sql
-- Pre-flight: verify IO waiters < 2 before each group
SELECT count(*) FILTER (WHERE state='active' AND wait_event_type='IO') FROM pg_stat_activity WHERE backend_type='client backend';

-- GROUP 1: HTML entity fixes (1,599 rows total — run all at once, tiny)
UPDATE leads SET niche_slug = 'heating_&_air_conditioning/hvac' WHERE niche_slug = 'heating_&amp;_air_conditioning/hvac';
UPDATE leads SET niche_slug = 'health_&_medical'          WHERE niche_slug = 'health_&amp;_medical';
UPDATE leads SET niche_slug = 'fitness_&_instruction'     WHERE niche_slug = 'fitness_&amp;_instruction';
UPDATE leads SET niche_slug = 'child_care_&_day_care'     WHERE niche_slug = 'child_care_&amp;_day_care';
UPDATE leads SET niche_slug = 'party_&_event_planning'    WHERE niche_slug = 'party_&amp;_event_planning';
UPDATE leads SET niche_slug = 'beauty_&_spas'             WHERE niche_slug = 'beauty_&amp;_spa';
UPDATE leads SET niche_slug = 'eyewear_&_opticians'       WHERE niche_slug = 'eyewear_&amp;_optician';
UPDATE leads SET niche_slug = 'banks_&_credit_unions'     WHERE niche_slug = 'bank_&amp;_credit_union';
UPDATE leads SET niche_slug = 'counseling_&_mental_health' WHERE niche_slug = 'counseling_&amp;_mental_health';
UPDATE leads SET niche_slug = 'auto_parts_&_supplies'     WHERE niche_slug = 'auto_part_&amp;_supply';
UPDATE leads SET niche_slug = 'event_planning_&_services' WHERE niche_slug = 'event_planning_&amp;_service';
UPDATE leads SET niche_slug = 'arts_&_entertainment'      WHERE niche_slug = 'art_&amp;_entertainment';
UPDATE leads SET niche_slug = 'venues_&_event_spaces'     WHERE niche_slug = 'venue_&amp;_event_space';
UPDATE leads SET niche_slug = 'fences_&_gates'            WHERE niche_slug = 'fence_&amp;_gate';

-- GROUP 2: Simple plurals (11,448 rows — do in sub-batches of ≤ 20k)
-- Batch 2A: dentists, electricians, architects, chiropractors (3,181 rows)
UPDATE leads SET niche_slug = 'dentist'       WHERE niche_slug = 'dentists';
UPDATE leads SET niche_slug = 'electrician'   WHERE niche_slug = 'electricians';
UPDATE leads SET niche_slug = 'architect'     WHERE niche_slug = 'architects';
UPDATE leads SET niche_slug = 'chiropractor'  WHERE niche_slug = 'chiropractors';

-- Batch 2B: lawyers, photographers, florists, veterinarians (2,754 rows)
UPDATE leads SET niche_slug = 'lawyer'        WHERE niche_slug = 'lawyers';
UPDATE leads SET niche_slug = 'photographer'  WHERE niche_slug = 'photographers';
UPDATE leads SET niche_slug = 'florist'       WHERE niche_slug = 'florists';
UPDATE leads SET niche_slug = 'veterinarian'  WHERE niche_slug = 'veterinarians';

-- Batch 2C: remaining plurals (check IO < 2 first)
UPDATE leads SET niche_slug = 'orthodontist'         WHERE niche_slug = 'orthodontists';
UPDATE leads SET niche_slug = 'optometrist'          WHERE niche_slug = 'optometrists';
UPDATE leads SET niche_slug = 'nutritionist'         WHERE niche_slug = 'nutritionists';
UPDATE leads SET niche_slug = 'dermatologist'        WHERE niche_slug = 'dermatologists';
UPDATE leads SET niche_slug = 'hair_salon'           WHERE niche_slug = 'hair_salons';
UPDATE leads SET niche_slug = 'tree_service'         WHERE niche_slug = 'tree_services';
UPDATE leads SET niche_slug = 'landscape_architect'  WHERE niche_slug = 'landscape_architects';
UPDATE leads SET niche_slug = 'structural_engineer'  WHERE niche_slug = 'structural_engineers';
UPDATE leads SET niche_slug = 'metal_fabricator'     WHERE niche_slug = 'metal_fabricators';
UPDATE leads SET niche_slug = 'mortgage_broker'      WHERE niche_slug = 'mortgage_brokers';
UPDATE leads SET niche_slug = 'mortgage_lender'      WHERE niche_slug = 'mortgage_lenders';
UPDATE leads SET niche_slug = 'pet_groomer'          WHERE niche_slug = 'pet_groomers';
UPDATE leads SET niche_slug = 'used_car_dealer'      WHERE niche_slug = 'used_car_dealers';
UPDATE leads SET niche_slug = 'car_dealer'           WHERE niche_slug IN ('car_dealers','car_dealership');
UPDATE leads SET niche_slug = 'medical_spa'          WHERE niche_slug = 'medical_spas';
UPDATE leads SET niche_slug = 'doctor'               WHERE niche_slug = 'doctors';
UPDATE leads SET niche_slug = 'caterer'              WHERE niche_slug = 'caterers';
UPDATE leads SET niche_slug = 'dj'                   WHERE niche_slug = 'djs';
UPDATE leads SET niche_slug = 'chimney_sweep'        WHERE niche_slug = 'chimney_sweeps';
UPDATE leads SET niche_slug = 'painter_and_decorator' WHERE niche_slug IN ('painters','painter');

-- GROUP 3: Close synonyms (11,758 rows — check IO < 2 first)
-- Batch 3A: roofing, legal (3,499 rows)
UPDATE leads SET niche_slug = 'roofing_service'        WHERE niche_slug = 'roofing_contractor';
UPDATE leads SET niche_slug = 'dentist'                WHERE niche_slug = 'dental_clinic';
UPDATE leads SET niche_slug = 'veterinarian'           WHERE niche_slug IN ('veterinary','veterinary_care');
UPDATE leads SET niche_slug = 'lawn_care_service'      WHERE niche_slug = 'lawn_services';
UPDATE leads SET niche_slug = 'personal_injury_lawyer' WHERE niche_slug IN ('personal_injury_attorney','personal_injury_law');
UPDATE leads SET niche_slug = 'bankruptcy_lawyer'      WHERE niche_slug = 'bankruptcy_law';

-- Batch 3B: service synonyms (check IO < 2 first)
UPDATE leads SET niche_slug = 'demolition_contractor'   WHERE niche_slug = 'demolition_services';
UPDATE leads SET niche_slug = 'excavating_contractor'   WHERE niche_slug = 'excavation_services';
UPDATE leads SET niche_slug = 'air_duct_cleaning_service' WHERE niche_slug = 'air_duct_cleaning';
UPDATE leads SET niche_slug = 'carpet_cleaning_service' WHERE niche_slug = 'carpet_cleaning';
UPDATE leads SET niche_slug = 'dumpster_rental_service' WHERE niche_slug = 'dumpster_rental';
UPDATE leads SET niche_slug = 'septic_tank_service'     WHERE niche_slug = 'septic_services';
UPDATE leads SET niche_slug = 'insulation_contractor'   WHERE niche_slug = 'insulation_installation';
UPDATE leads SET niche_slug = 'wedding_planner'         WHERE niche_slug = 'wedding_planning';
UPDATE leads SET niche_slug = 'movers'                  WHERE niche_slug = 'mover';
UPDATE leads SET niche_slug = 'removals_company'        WHERE niche_slug = 'removals_service';
UPDATE leads SET niche_slug = 'snow_removal_service'    WHERE niche_slug = 'snow_removal';
UPDATE leads SET niche_slug = 'pressure_washing_service' WHERE niche_slug = 'pressure_washers';
UPDATE leads SET niche_slug = 'pool_cleaning_service'   WHERE niche_slug = 'pool_cleaners';
UPDATE leads SET niche_slug = 'garage_door_services'    WHERE niche_slug = 'garage_door_service';
UPDATE leads SET niche_slug = 'acupuncture_clinic'      WHERE niche_slug IN ('acupuncturist','acupuncture');
UPDATE leads SET niche_slug = 'limousine_service'       WHERE niche_slug IN ('limos','limo','limo_service');
UPDATE leads SET niche_slug = 'estate_agent'            WHERE niche_slug IN ('real_estate_agent','real_estate_agents','estate_agents');
```

---

## LARGE/RISKY OPERATIONS — NEEDS JIM SIGN-OFF

| Operation | Rows | Risk |
|-----------|------|------|
| Merge `realestate` → `estate_agent` | 34,480 | Large batch; `realestate` verified clean but big |
| Reclassify `roofing` clean rows → `roofing_service` | ~12,055 | Requires WHERE category filter — see recommendation |
| Reclassify `hvac` clean rows → `hvac_contractor` | ~18,084 | Same |
| Reclassify `plumbing` clean rows → `plumber` | ~11,000 | Same |
| Reclassify `contractor`/`contractors` | 9,103 | All-null categories — cannot verify without name review |
| Merge `dental` → `dentist` | 16,119 | Verify it doesn't include dental labs/supply |

---

## SLUGS TO KEEP SEPARATE (not to merge)

These are distinct services that merely sound related:
- `plumbers_merchant` (436) — supply store, not a plumber
- `wedding_photographer` (3,385), `commercial_photographer` (562), `aerial_photographer` (167) — distinct sub-niches
- `landscape_designer` (2,730), `landscape_company` (459), `landscape_architect` (1,116) — different specialties
- `chiropodist` (828) — UK term for podiatrist; different from chiropractor
- `architecture_firm` (1,238) — firm, not individual architect
- `orthodontist` (2,491), `cosmetic_dentist` (776), `paediatric_dentist` (831) — dental specialties, keep distinct
- `law_firm` (3,084) — distinct from `lawyer` (individual vs firm)
- `insurance_agency` (7,170), `insurance_broker` (528), `insurance_company` (347) — different business models
- `garage_door_supplier` (1,709) — supply vs service
