WITH source_rows AS (
  SELECT
    geom,
    ts018_0004,
    ts018_0001,
    SUM(COALESCE(ts018_0004, 0.0)) OVER ()::double precision AS national_arrived_age_0_4,
    SUM(COALESCE(ts018_0001, 0.0)) OVER ()::double precision AS national_total_population
  FROM leeds.census_2021_england_oa
),
scope_cells AS (
  SELECT h3_polygon_to_cells(
           _mapp_h3_scope.geom_4326,
           9
         ) AS h3_id
  FROM _mapp_h3_scope
),
cells AS (
  SELECT h3_id,
         public.ST_GeomFromWKB(h3_cell_to_boundary_wkb(h3_id), 4326) AS geom_4326
  FROM scope_cells
),
weighted AS (
  SELECT
    cell.h3_id,
    cell.geom_4326,
    SUM(
      COALESCE(oa.ts018_0004, 0.0)
      * public.ST_Area(public.ST_Transform(public.ST_Intersection(oa.geom, cell.geom_4326), 27700))
      / NULLIF(public.ST_Area(public.ST_Transform(oa.geom, 27700)), 0.0)
    )::double precision AS arrived_age_0_4,
    SUM(
      COALESCE(oa.ts018_0001, 0.0)
      * public.ST_Area(public.ST_Transform(public.ST_Intersection(oa.geom, cell.geom_4326), 27700))
      / NULLIF(public.ST_Area(public.ST_Transform(oa.geom, 27700)), 0.0)
    )::double precision AS total_population,
    MAX(oa.national_arrived_age_0_4)::double precision AS national_arrived_age_0_4,
    MAX(oa.national_total_population)::double precision AS national_total_population
  FROM cells AS cell
  JOIN source_rows AS oa
    ON public.ST_Intersects(oa.geom, cell.geom_4326)
  GROUP BY cell.h3_id, cell.geom_4326
),
metrics AS (
  SELECT
    weighted.*,
    100.0 * weighted.arrived_age_0_4
      / NULLIF(weighted.total_population, 0.0) AS local_percent,
    100.0 * weighted.national_arrived_age_0_4
      / NULLIF(weighted.national_total_population, 0.0) AS national_average_percent
  FROM weighted
)
SELECT
  h3_id::text AS h3_id,
  arrived_age_0_4,
  total_population,
  local_percent,
  national_average_percent,
  CASE
    WHEN local_percent > national_average_percent THEN 'Over national average'
    WHEN local_percent < national_average_percent THEN 'Under national average'
    ELSE 'At national average'
  END::text AS divergence,
  to_char(round(arrived_age_0_4::numeric, 1), 'FM999,999,999,990.0')::text AS arrived_age_0_4_display,
  to_char(round(total_population::numeric, 1), 'FM999,999,999,990.0')::text AS total_population_display,
  (to_char(round(arrived_age_0_4::numeric, 1), 'FM999,999,999,990.0') || ' residents')::text AS hover_text,
  public.ST_Transform(geom_4326, 3857)::public.geometry(Polygon, 3857) AS geom_3857
FROM metrics
WHERE total_population > 0.0
