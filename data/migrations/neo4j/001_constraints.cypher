// One node per entity per type. Entity.id is canonical "{type}:{normalized_name}".
CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
FOR (e:Entity) REQUIRE e.id IS UNIQUE;

CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS
FOR (c:Chunk) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT community_id_unique IF NOT EXISTS
FOR (c:Community) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT document_id_unique IF NOT EXISTS
FOR (d:Document) REQUIRE d.id IS UNIQUE;

// Lookup helpers
CREATE INDEX entity_type_idx IF NOT EXISTS FOR (e:Entity) ON (e.type);
CREATE INDEX entity_name_idx IF NOT EXISTS FOR (e:Entity) ON (e.name);
