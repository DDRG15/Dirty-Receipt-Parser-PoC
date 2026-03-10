Titanium-Vault: Dirty Receipt Parser PoC (V13.6)
Architect: Diego del Rio Garcia


🏛️ Project Overview
This isn't a "Happy Path" parser. It is a high-performance, crash-resilient ingestion engine built to handle "dirty" OCR data while surviving the absolute chaos of OS-level process termination.

Developer Note: I spent 3 hours questioning my own sanity before realizing the code was fine—the Windows Kernel was the problem. It turns out that sometimes, it really isn't me, it's you, Windows. I should have used Linux, and I definitely should have used Docker from the start. Lesson learned in blood, sweat, and tears.
⚔️ Key Engineering Challenges Solved
1. The Dual-Write Paradox (The "Headache" Pattern)
Writing to a database and a flat-file at the same time is a mathematical suicide mission. If the system crashes between those two writes, your data is garbage. I didn't see it at first, but once I did, I fixed it permanently.

Solution: Transactional Outbox Pattern. The SQLite database is the only "Source of Truth." It commits the full JSON payload in an ACID transaction first. The files are exported atomically only after the vault is sealed.
2. Windows NTFS Hardening (The WinError 32 Graveyard)
Windows handles file metadata like it's 1995. I fought the WinError 32 (Mandatory Locking) so you don't have to.

VDL Protection: Most developers trust file.flush(). They are wrong. On Windows, NTFS will truncate your file to 0-bytes on a hard crash unless you force the Valid Data Length (VDL) pointer to advance. I used os.fsync() to create a hardware-level barrier.
Kernel Locking: Implemented explicit connection closures and WAL-truncation to stop the Windows Kernel from "babysitting" files and blocking the pipeline.
3. Backpressure & Memory Safety (OOM Prevention)
Processing a 50GB file on 8GB of RAM shouldn't be a miracle—it should be a requirement.

Solution: Capped MAX_INFLIGHT task queue. The producer stops reading when the workers are full. Memory usage stays flat, performance stays high.
🚦 Quick Start
Don't just take my word for it. Run the chaos tests and try to break it.

Initialize: python nuclear.py (The Big Bang)
Install: pip install -r requirements.txt
Validate: pytest tests/ -v (The 55-Test Gauntlet)
Chaos Test: python scripts/crash_restart_test.py (The "Hard Kill" Proof)