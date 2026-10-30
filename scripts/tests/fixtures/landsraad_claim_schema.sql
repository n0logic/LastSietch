-- Throwaway-postgres fixture for scripts/tests/test_landsraad_claim_pg.sh.
-- GENERATED 2026-10-06 from the LIVE game DB, not hand-written (Working Rule 5: a fixture
-- that does not match production is not a test of production):
--   pg_dump -s of the tables the claim writer touches, the enum/composite types they use,
--   and pg_get_functiondef of landsraad_withdraw_house_reward + the notify trigger fn.
-- Only removed: owner statements, one trigram index, and foreign keys to tables outside
-- this set. encrypted_accounts + decrypt_user_data are stubs (the real ones hold keys).
-- Regenerate after a Funcom migration touches any of these.
CREATE SCHEMA IF NOT EXISTS dune;
CREATE TYPE dune.actorstate AS ENUM ('Default', 'Travel', 'VehicleBackup', 'AbortedAuthorityTransfer', 'VehicleRecovery', 'BaseBackup', 'SimulatedLandsraadActor');
CREATE TYPE dune.characterstate AS ENUM ('Active', 'Deleted');
CREATE TYPE dune.playerconnectionstatus AS ENUM ('Offline', 'LoggingOut', 'Online');
CREATE TYPE dune.playerlifestate AS ENUM ('Alive', 'Dead', 'DeadByCoriolis', 'DeadBySandworm');
CREATE TYPE dune.vector AS (x double precision, y double precision, z double precision);
CREATE TYPE dune.quaternion AS (x double precision, y double precision, z double precision, w double precision);
CREATE TYPE dune.transform AS (location dune.vector, rotation dune.quaternion);
CREATE TYPE dune.deathlocation AS (location dune.vector, map text, dimension integer);
CREATE TYPE dune.landsraadplayerhousereward AS (house_name text, template_id text, amount integer);
CREATE TABLE dune.encrypted_accounts (id bigint PRIMARY KEY, "user" text, encrypted_funcom_id bytea,
  takeoverable boolean, encrypted_platform_id bytea, platform_name text);
CREATE FUNCTION dune.decrypt_user_data(bytea) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT convert_from($1, 'UTF8') $$;
-- the native procs name their types unqualified, as on live
SET search_path TO dune, public;

CREATE OR REPLACE FUNCTION dune.landsraad_withdraw_house_reward(in_player_id bigint, in_house_rewards dune.landsraadplayerhousereward[])
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE
	current_amount INTEGER = NULL;
	house_reward record = NULL;
    grouped_house_rewards LandsraadPlayerHouseReward[];
BEGIN
	LOCK TABLE landsraad_house_rewards IN EXCLUSIVE MODE;
    --group rewards to make sure multiple entries of the same item do not slip by amount verfication below
    WITH grouped_rewards AS (SELECT house_name, template_id, SUM(amount) as amount FROM UNNEST(in_house_rewards) GROUP BY house_name, template_id)
        SELECT ARRAY_AGG((grouped_rewards.house_name, grouped_rewards.template_id, grouped_rewards.amount)::LandsraadPlayerHouseReward) INTO grouped_house_rewards FROM grouped_rewards;
    
	FOREACH house_reward in ARRAY grouped_house_rewards
	LOOP
		SELECT lhr.amount INTO current_amount FROM landsraad_house_rewards AS lhr WHERE lhr.player_id = in_player_id AND lhr.house_name = house_reward.house_name AND lhr.template_id = house_reward.template_id;

		IF current_amount IS NULL OR current_amount < house_reward.amount THEN
			RAISE EXCEPTION 'Cannot withdraw house reward %s for player % and house %s', house_reward.template_id, in_player_id, house_reward.house_name;
			RETURN;
		END IF;
	END LOOP;
	-- finish full loop of checks first, all rewards need to be withdrawable before updating
	FOREACH house_reward in ARRAY grouped_house_rewards
	LOOP
		UPDATE landsraad_house_rewards SET amount = amount - house_reward.amount, last_updated = CURRENT_TIMESTAMP WHERE player_id = in_player_id AND house_name = house_reward.house_name AND template_id = house_reward.template_id;
	END LOOP;
END $function$
;
CREATE OR REPLACE FUNCTION dune.landsraad_notify_house_rewards_changed()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
	PERFORM pg_notify('landsraad_notify_channel', format('house_rewards_changed#{"PlayerId" : %s}', NEW.player_id));
    RETURN NULL;
END $function$
;

--
-- PostgreSQL database dump
--

-- Dumped from database version 17.4
-- Dumped by pg_dump version 17.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: encrypted_player_state; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.encrypted_player_state (
    account_id bigint NOT NULL,
    encrypted_character_name bytea NOT NULL,
    last_avatar_activity timestamp with time zone,
    server_id text,
    player_controller_id bigint,
    player_pawn_id bigint,
    player_state_id bigint,
    pending_respawn_location_id uuid,
    life_state dune.playerlifestate DEFAULT 'Alive'::dune.playerlifestate NOT NULL,
    online_status dune.playerconnectionstatus DEFAULT 'Offline'::dune.playerconnectionstatus NOT NULL,
    reconnect_grace_period_end timestamp without time zone,
    previous_server_partition_id bigint,
    is_coriolis_processed boolean DEFAULT true NOT NULL,
    return_dimension_index integer,
    death_location dune.deathlocation,
    home_dimension_index integer,
    logoff_persistence_end_time timestamp without time zone,
    last_login_time timestamp with time zone,
    last_returning_player_event_time timestamp with time zone,
    last_returning_player_awarded_time timestamp with time zone,
    id bigint NOT NULL,
    character_state dune.characterstate DEFAULT 'Active'::dune.characterstate NOT NULL,
    last_character_state_change timestamp without time zone DEFAULT now() NOT NULL,
    transfer_count integer DEFAULT 0 NOT NULL
);



--
-- Name: actors; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.actors (
    id bigint NOT NULL,
    class text,
    map text,
    transform dune.transform,
    partition_id bigint,
    dimension_index integer DEFAULT 0 NOT NULL,
    gas_attributes jsonb DEFAULT '{}'::jsonb NOT NULL,
    properties jsonb DEFAULT '{}'::jsonb NOT NULL,
    owner_account_id bigint,
    serial bigint DEFAULT 0 NOT NULL,
    state dune.actorstate DEFAULT 'Default'::dune.actorstate NOT NULL,
    CONSTRAINT actors_id_check CHECK ((id > 0))
);



--
-- Name: accounts; Type: VIEW; Schema: dune; Owner: dune
--

CREATE VIEW dune.accounts AS
 SELECT id,
    "user",
    dune.decrypt_user_data(encrypted_funcom_id) AS funcom_id,
    takeoverable,
    dune.decrypt_user_data(encrypted_platform_id) AS platform_id,
    platform_name
   FROM dune.encrypted_accounts;



--
-- Name: inventories; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.inventories (
    id bigint NOT NULL,
    actor_id bigint,
    exchange_id bigint,
    item_id bigint,
    vehicle_module_id bigint,
    inventory_type integer,
    max_item_count integer,
    max_item_volume real,
    CONSTRAINT inventories_id_check CHECK ((id > 0)),
    CONSTRAINT valid_fkey CHECK (((actor_id IS NOT NULL) OR (exchange_id IS NOT NULL) OR (item_id IS NOT NULL) OR (vehicle_module_id IS NOT NULL)))
);



--
-- Name: items; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.items (
    id bigint NOT NULL,
    inventory_id bigint,
    stack_size bigint NOT NULL,
    position_index bigint NOT NULL,
    template_id text NOT NULL,
    is_new boolean DEFAULT true,
    acquisition_time bigint DEFAULT 0 NOT NULL,
    stats jsonb NOT NULL,
    quality_level bigint DEFAULT 0 NOT NULL,
    volume_override real,
    CONSTRAINT items_position_index_check CHECK ((position_index >= 0)),
    CONSTRAINT items_stack_size_check CHECK ((stack_size > 0))
);



--
-- Name: actors_id_seq; Type: SEQUENCE; Schema: dune; Owner: dune
--

CREATE SEQUENCE dune.actors_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: actors_id_seq; Type: SEQUENCE OWNED BY; Schema: dune; Owner: dune
--

ALTER SEQUENCE dune.actors_id_seq OWNED BY dune.actors.id;


--
-- Name: encrypted_player_state_id_seq; Type: SEQUENCE; Schema: dune; Owner: dune
--

CREATE SEQUENCE dune.encrypted_player_state_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: encrypted_player_state_id_seq; Type: SEQUENCE OWNED BY; Schema: dune; Owner: dune
--

ALTER SEQUENCE dune.encrypted_player_state_id_seq OWNED BY dune.encrypted_player_state.id;


--
-- Name: dune_exchange_fulfilled_orders; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.dune_exchange_fulfilled_orders (
    order_id bigint NOT NULL,
    completion_type integer NOT NULL,
    stack_size bigint NOT NULL,
    source_order_id bigint,
    original_order_id bigint,
    CONSTRAINT dune_exchange_fulfilled_orders_stack_size_check CHECK ((stack_size > 0))
);



--
-- Name: dune_exchange_orders; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.dune_exchange_orders (
    id bigint NOT NULL,
    revision bigint DEFAULT 1 NOT NULL,
    exchange_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    item_id bigint,
    template_id text NOT NULL,
    category_mask integer NOT NULL,
    category_depth smallint NOT NULL,
    access_point_id bigint NOT NULL,
    is_npc_order boolean DEFAULT false NOT NULL,
    item_price bigint NOT NULL,
    expiration_time bigint,
    durability_cur real NOT NULL,
    durability_max real NOT NULL,
    quality_level bigint DEFAULT 0 NOT NULL,
    CONSTRAINT dune_exchange_orders_id_check CHECK ((id > 0)),
    CONSTRAINT dune_exchange_orders_id_check1 CHECK ((id > 0))
);



--
-- Name: dune_exchange_orders_id_seq; Type: SEQUENCE; Schema: dune; Owner: dune
--

CREATE SEQUENCE dune.dune_exchange_orders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: dune_exchange_orders_id_seq; Type: SEQUENCE OWNED BY; Schema: dune; Owner: dune
--

ALTER SEQUENCE dune.dune_exchange_orders_id_seq OWNED BY dune.dune_exchange_orders.id;


--
-- Name: farm_variables; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.farm_variables (
    universe_time_timestamp timestamp without time zone,
    farm_id text,
    universe_lastactive_timestamp timestamp without time zone,
    down_time_accumulation bigint DEFAULT 0,
    one_row boolean DEFAULT true NOT NULL,
    battlegroup_close_date timestamp without time zone
);



--
-- Name: ls_item_delivery_log; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.ls_item_delivery_log (
    id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    correlation_id text NOT NULL,
    source text NOT NULL,
    lane text NOT NULL,
    account_id bigint,
    fls_id text,
    actor_id bigint,
    template_id text NOT NULL,
    quantity integer NOT NULL,
    quality_level integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    delivered_at timestamp with time zone,
    observed_item_id bigint,
    observed_stack_delta bigint,
    poll_attempts integer,
    error text,
    operator text,
    CONSTRAINT ls_item_delivery_log_lane_chk CHECK ((lane = ANY (ARRAY['rmq'::text, 'db'::text, 'exchange'::text]))),
    CONSTRAINT ls_item_delivery_log_status_chk CHECK ((status = ANY (ARRAY['pending'::text, 'delivered'::text, 'undelivered'::text, 'failed'::text])))
);



--
-- Name: COLUMN ls_item_delivery_log.status; Type: COMMENT; Schema: dune; Owner: dune
--

COMMENT ON COLUMN dune.ls_item_delivery_log.status IS 'pending = published/listed, not yet in the player''s hands; delivered = confirmed in dune.items; undelivered = window elapsed; failed = the write itself failed';


--
-- Name: ls_item_delivery_log_id_seq; Type: SEQUENCE; Schema: dune; Owner: dune
--

CREATE SEQUENCE dune.ls_item_delivery_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: ls_item_delivery_log_id_seq; Type: SEQUENCE OWNED BY; Schema: dune; Owner: dune
--

ALTER SEQUENCE dune.ls_item_delivery_log_id_seq OWNED BY dune.ls_item_delivery_log.id;


--
-- Name: inventories_id_seq; Type: SEQUENCE; Schema: dune; Owner: dune
--

CREATE SEQUENCE dune.inventories_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: inventories_id_seq; Type: SEQUENCE OWNED BY; Schema: dune; Owner: dune
--

ALTER SEQUENCE dune.inventories_id_seq OWNED BY dune.inventories.id;


--
-- Name: items_id_seq; Type: SEQUENCE; Schema: dune; Owner: dune
--

CREATE SEQUENCE dune.items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: items_id_seq; Type: SEQUENCE OWNED BY; Schema: dune; Owner: dune
--

ALTER SEQUENCE dune.items_id_seq OWNED BY dune.items.id;


--
-- Name: landsraad_house_rewards; Type: TABLE; Schema: dune; Owner: dune
--

CREATE TABLE dune.landsraad_house_rewards (
    player_id bigint NOT NULL,
    house_name text NOT NULL,
    amount integer DEFAULT 0 NOT NULL,
    template_id text NOT NULL,
    last_updated timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--
-- Name: actors id; Type: DEFAULT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.actors ALTER COLUMN id SET DEFAULT nextval('dune.actors_id_seq'::regclass);


--
-- Name: dune_exchange_orders id; Type: DEFAULT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.dune_exchange_orders ALTER COLUMN id SET DEFAULT nextval('dune.dune_exchange_orders_id_seq'::regclass);


--
-- Name: encrypted_player_state id; Type: DEFAULT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.encrypted_player_state ALTER COLUMN id SET DEFAULT nextval('dune.encrypted_player_state_id_seq'::regclass);


--
-- Name: ls_item_delivery_log id; Type: DEFAULT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.ls_item_delivery_log ALTER COLUMN id SET DEFAULT nextval('dune.ls_item_delivery_log_id_seq'::regclass);


--
-- Name: inventories id; Type: DEFAULT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.inventories ALTER COLUMN id SET DEFAULT nextval('dune.inventories_id_seq'::regclass);


--
-- Name: items id; Type: DEFAULT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.items ALTER COLUMN id SET DEFAULT nextval('dune.items_id_seq'::regclass);


--
-- Name: actors actors_pkey; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.actors
    ADD CONSTRAINT actors_pkey PRIMARY KEY (id);


--
-- Name: dune_exchange_orders dune_exchange_orders_item_id_unique; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.dune_exchange_orders
    ADD CONSTRAINT dune_exchange_orders_item_id_unique UNIQUE (item_id);


--
-- Name: dune_exchange_orders dune_exchange_orders_pkey; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.dune_exchange_orders
    ADD CONSTRAINT dune_exchange_orders_pkey PRIMARY KEY (id);


--
-- Name: encrypted_player_state encrypted_player_state_pkey; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.encrypted_player_state
    ADD CONSTRAINT encrypted_player_state_pkey PRIMARY KEY (id);


--
-- Name: ls_item_delivery_log ls_item_delivery_log_correlation_id_key; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.ls_item_delivery_log
    ADD CONSTRAINT ls_item_delivery_log_correlation_id_key UNIQUE (correlation_id);


--
-- Name: ls_item_delivery_log ls_item_delivery_log_pkey; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.ls_item_delivery_log
    ADD CONSTRAINT ls_item_delivery_log_pkey PRIMARY KEY (id);


--
-- Name: inventories inventories_pkey; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.inventories
    ADD CONSTRAINT inventories_pkey PRIMARY KEY (id);


--
-- Name: items items_pkey; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.items
    ADD CONSTRAINT items_pkey PRIMARY KEY (id);


--
-- Name: landsraad_house_rewards landsraad_house_rewards_player_id_unique; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.landsraad_house_rewards
    ADD CONSTRAINT landsraad_house_rewards_player_id_unique UNIQUE (player_id, house_name, template_id);


--
-- Name: farm_variables one_row_pk; Type: CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.farm_variables
    ADD CONSTRAINT one_row_pk PRIMARY KEY (one_row);


--
-- Name: actors_map_dimension_ownerless_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX actors_map_dimension_ownerless_idx ON dune.actors USING btree (map, dimension_index) WHERE (owner_account_id IS NULL);


--
-- Name: actors_owner_account_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX actors_owner_account_id_idx ON dune.actors USING btree (((owner_account_id IS NULL)));


--
-- Name: dune_exchange_orders_category_depth_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX dune_exchange_orders_category_depth_idx ON dune.dune_exchange_orders USING btree (category_depth);


--
-- Name: dune_exchange_orders_exchange_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX dune_exchange_orders_exchange_id_idx ON dune.dune_exchange_orders USING btree (exchange_id);


--
-- Name: dune_exchange_orders_expr_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX dune_exchange_orders_expr_idx ON dune.dune_exchange_orders USING btree (((category_mask >> 0)));


--
-- Name: dune_exchange_orders_expr_idx1; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX dune_exchange_orders_expr_idx1 ON dune.dune_exchange_orders USING btree (((category_mask >> 8)));


--
-- Name: dune_exchange_orders_expr_idx2; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX dune_exchange_orders_expr_idx2 ON dune.dune_exchange_orders USING btree (((category_mask >> 16)));


--
-- Name: dune_exchange_orders_expr_idx3; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX dune_exchange_orders_expr_idx3 ON dune.dune_exchange_orders USING btree (((category_mask >> 24)));


--
-- Name: dune_exchange_orders_template_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX dune_exchange_orders_template_id_idx ON dune.dune_exchange_orders USING btree (template_id);


--
-- Name: encrypted_player_state_account_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_account_id_idx ON dune.encrypted_player_state USING btree (account_id);


--
-- Name: encrypted_player_state_character_state_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_character_state_idx ON dune.encrypted_player_state USING btree (character_state);


--
-- Name: encrypted_player_state_encrypted_character_name_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_encrypted_character_name_idx ON dune.encrypted_player_state USING btree (encrypted_character_name) WHERE (character_state = 'Active'::dune.characterstate);


--
-- Name: encrypted_player_state_logoff_persistence_expiry_time_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_logoff_persistence_expiry_time_idx ON dune.encrypted_player_state USING btree (logoff_persistence_end_time);


--
-- Name: encrypted_player_state_pending_respawn_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_pending_respawn_idx ON dune.encrypted_player_state USING btree (pending_respawn_location_id) WHERE (pending_respawn_location_id IS NOT NULL);


--
-- Name: encrypted_player_state_player_controller_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_player_controller_id_idx ON dune.encrypted_player_state USING btree (player_controller_id);


--
-- Name: encrypted_player_state_player_pawn_id_fk_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_player_pawn_id_fk_idx ON dune.encrypted_player_state USING btree (player_pawn_id);


--
-- Name: encrypted_player_state_player_state_id_fk_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_player_state_id_fk_idx ON dune.encrypted_player_state USING btree (player_state_id);


--
-- Name: encrypted_player_state_reconnect_grace_period_end_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX encrypted_player_state_reconnect_grace_period_end_idx ON dune.encrypted_player_state USING btree (reconnect_grace_period_end);


--
-- Name: ls_item_delivery_log_account_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX ls_item_delivery_log_account_idx ON dune.ls_item_delivery_log USING btree (account_id, created_at DESC);


--
-- Name: ls_item_delivery_log_status_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX ls_item_delivery_log_status_idx ON dune.ls_item_delivery_log USING btree (status, created_at DESC);


--
-- Name: idx_inventory_id; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX idx_inventory_id ON dune.items USING btree (inventory_id);


--
-- Name: idx_item_id; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX idx_item_id ON dune.inventories USING btree (item_id);


--
-- Name: inventory_actor_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX inventory_actor_id_idx ON dune.inventories USING btree (actor_id);


--
-- Name: inventory_item_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX inventory_item_id_idx ON dune.inventories USING hash (item_id);


--
-- Name: item_inventory_id_idx; Type: INDEX; Schema: dune; Owner: dune
--

CREATE INDEX item_inventory_id_idx ON dune.items USING hash (inventory_id);


--
-- Name: landsraad_house_rewards landsraad_tasks_house_rewards_changed; Type: TRIGGER; Schema: dune; Owner: dune
--

CREATE TRIGGER landsraad_tasks_house_rewards_changed AFTER INSERT OR UPDATE ON dune.landsraad_house_rewards FOR EACH ROW EXECUTE FUNCTION dune.landsraad_notify_house_rewards_changed();


--
-- Name: dune_exchange_fulfilled_orders dune_exchange_fulfilled_orders_order_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.dune_exchange_fulfilled_orders
    ADD CONSTRAINT dune_exchange_fulfilled_orders_order_id_fkey FOREIGN KEY (order_id) REFERENCES dune.dune_exchange_orders(id) ON DELETE CASCADE;


--
-- Name: dune_exchange_fulfilled_orders dune_exchange_fulfilled_orders_source_order_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.dune_exchange_fulfilled_orders
    ADD CONSTRAINT dune_exchange_fulfilled_orders_source_order_id_fkey FOREIGN KEY (source_order_id) REFERENCES dune.dune_exchange_orders(id) ON DELETE SET NULL;


--
-- Name: dune_exchange_orders dune_exchange_orders_item_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.dune_exchange_orders
    ADD CONSTRAINT dune_exchange_orders_item_id_fkey FOREIGN KEY (item_id) REFERENCES dune.items(id) ON DELETE CASCADE;


--
-- Name: dune_exchange_orders dune_exchange_orders_owner_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.dune_exchange_orders
    ADD CONSTRAINT dune_exchange_orders_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES dune.actors(id) ON DELETE CASCADE;


--
-- Name: encrypted_player_state encrypted_player_state_account_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.encrypted_player_state
    ADD CONSTRAINT encrypted_player_state_account_id_fkey FOREIGN KEY (account_id) REFERENCES dune.encrypted_accounts(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: encrypted_player_state encrypted_player_state_player_controller_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.encrypted_player_state
    ADD CONSTRAINT encrypted_player_state_player_controller_id_fkey FOREIGN KEY (player_controller_id) REFERENCES dune.actors(id) ON DELETE SET NULL;


--
-- Name: encrypted_player_state encrypted_player_state_player_pawn_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.encrypted_player_state
    ADD CONSTRAINT encrypted_player_state_player_pawn_id_fkey FOREIGN KEY (player_pawn_id) REFERENCES dune.actors(id) ON DELETE SET NULL;


--
-- Name: encrypted_player_state encrypted_player_state_player_state_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.encrypted_player_state
    ADD CONSTRAINT encrypted_player_state_player_state_id_fkey FOREIGN KEY (player_state_id) REFERENCES dune.actors(id) ON DELETE SET NULL;


--
-- Name: inventories inventories_actor_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.inventories
    ADD CONSTRAINT inventories_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES dune.actors(id) ON DELETE CASCADE;


--
-- Name: inventories inventories_item_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.inventories
    ADD CONSTRAINT inventories_item_id_fkey FOREIGN KEY (item_id) REFERENCES dune.items(id) ON DELETE CASCADE;


--
-- Name: items items_inventory_id_fkey; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.items
    ADD CONSTRAINT items_inventory_id_fkey FOREIGN KEY (inventory_id) REFERENCES dune.inventories(id) ON DELETE CASCADE;


--
-- Name: landsraad_house_rewards landsraad_house_rewards_player_id_fk; Type: FK CONSTRAINT; Schema: dune; Owner: dune
--

ALTER TABLE ONLY dune.landsraad_house_rewards
    ADD CONSTRAINT landsraad_house_rewards_player_id_fk FOREIGN KEY (player_id) REFERENCES dune.actors(id) ON DELETE CASCADE;

