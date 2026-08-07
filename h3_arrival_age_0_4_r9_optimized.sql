source_areas AS (
  SELECT
    oa.ts018_0004,
    oa.ts018_0001,
    public.ST_Transform(oa.geom, 27700) AS geom_27700,
    public.ST_Area(public.ST_Transform(oa.geom, 27700)) AS area_m2
  FROM leeds.census_2021_england_oa AS oa
  WHERE oa.geom IS NOT NULL
    AND oa.ts018_0004 IS NOT NULL
    AND oa.ts018_0001 IS NOT NULL
    AND public.ST_Intersects(oa.geom, public.ST_MakeEnvelope(-2.867459375, 53.360537816492, -0.230740625, 54.236487833856, 4326))
),
scope_cells AS (
  SELECT h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 9) AS h3_id
  FROM _mapp_h3_scope
),
cells AS (
  SELECT
    scope_cells.h3_id,
    public.ST_GeomFromWKB(
      h3_cell_to_boundary_wkb(scope_cells.h3_id),
      4326
    )::public.geometry(Polygon, 4326) AS geom_4326
  FROM scope_cells
),
projected_cells AS (
  SELECT
    h3_id,
    geom_4326,
    public.ST_Transform(geom_4326, 27700) AS geom_27700
  FROM cells
),
cell_overlaps AS (
  SELECT
    cell.h3_id,
    cell.geom_4326,
    source.ts018_0004,
    source.ts018_0001,
    public.ST_Area(
      public.ST_Intersection(source.geom_27700, cell.geom_27700)
    ) AS overlap_m2,
    source.area_m2
  FROM projected_cells AS cell
  JOIN source_areas AS source
    ON public.ST_Intersects(source.geom_27700, cell.geom_27700)
),
weighted AS (
  SELECT
    overlap.h3_id,
metrics AS (
  SELECT
    weighted.*,
    100.0 * weighted.arrived_age_0_4 / NULLIF(weighted.total_population, 0.0) AS local_percent,
    100.0 * (SELECT SUM(COALESCE(ts018_0004, 0.0)) FROM leeds.census_2021_england_oa) /
      NULLIF((SELECT SUM(COALESCE(ts018_0001, 0.0)) FROM leeds.census_2021_england_oa), 0.0) AS national_average_percent
  FROM weighted
)
    100.0 * national.national_arrived_age_0_4 / NULLIF(national.national_total_population, 0.0) AS national_average_percent
  FROM weighted
  JOIN national_totals AS national
    ON national.national_total_population IS NOT NULL
)
SELECT
  h3_id::text AS h3_id,
  9 AS h3_resolution,
  arrived_age_0_4,
  total_population,
  local_percent,
  national_average_percent,
  CASE
    WHEN local_percent < national_average_percent * 0.50 THEN 'Below 50% of England average'
    WHEN local_percent < national_average_percent * 0.75 THEN '50–75% of England average'
    WHEN local_percent < national_average_percent THEN '75–100% of England average'
    WHEN local_percent < national_average_percent * 1.25 THEN '100–125% of England average'
    ELSE 'At least 125% of England average'
  END::text AS percentage_band,
  to_char(round(arrived_age_0_4::numeric, 1), 'FM999G999G999G990D0')::text AS arrived_age_0_4_display,
  to_char(round(total_population::numeric, 1), 'FM999G999G999G990D0')::text AS total_population_display,
  to_char(round(local_percent::numeric, 1), 'FM990D0') || '%' AS local_percent_display,
  to_char(round(national_average_percent::numeric, 1), 'FM990D0') || '%' AS national_average_display,
  ('Arrived aged 0–4: ' || to_char(round(arrived_age_0_4::numeric, 1), 'FM999G999G999G990D0') ||
    ' (' || to_char(round(local_percent::numeric, 1), 'FM990D0') || '%)')::text AS hover_text,
  public.ST_Transform(geom_4326, 3857)::public.geometry(Polygon, 3857) AS geom_3857
FROM metrics
WHERE total_population > 0.0
