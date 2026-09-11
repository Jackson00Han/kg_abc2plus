// Upload leases are coordination records, never document/source placeholders.
CREATE CONSTRAINT upload_reservation_id_unique IF NOT EXISTS
FOR (reservation:UploadReservation) REQUIRE reservation.reservation_id IS UNIQUE;
