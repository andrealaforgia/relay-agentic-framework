-- Atomic gap-free sequence + append.
-- KEYS[1] = seq counter key (relay:<swarm>:seq)
-- KEYS[2] = ledger stream key (relay:<swarm>:ledger)
-- ARGV    = flattened envelope field/value pairs, WITHOUT 'seq'
-- Returns { seq, stream_id }
--
-- INCR and XADD in one script means a crash between "take a number" and
-- "write the entry" is impossible: a gap in seq is therefore always an
-- audit signal, never an artifact of the transport.
local seq = redis.call('INCR', KEYS[1])
local args = { KEYS[2], '*', 'seq', tostring(seq) }
for i = 1, #ARGV do
  args[#args + 1] = ARGV[i]
end
local id = redis.call('XADD', unpack(args))
return { seq, id }
