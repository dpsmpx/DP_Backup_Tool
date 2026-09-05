import os
import json
import logging
import tkinter as tk
from tkinter import filedialog, simpledialog
import shutil

# --- Logging Configuration ---
def setup_logging():
    """Sets up logging to file for errors and conflicts."""
    logger_instance = logging.getLogger("BackupTool")
    if logger_instance.hasHandlers():
        logger_instance.handlers.clear()

    logger_instance.setLevel(logging.WARNING) # User requested only errors and conflicts
    fh = logging.FileHandler("backup_tool.log", encoding="utf-8", mode='a') # Append mode for user
    fh.setLevel(logging.WARNING)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    fh.setFormatter(formatter)
    logger_instance.addHandler(fh)
    return logger_instance

logger = setup_logging()

# --- Tkinter Helper Functions ---
def select_directory(title="Select Directory"):
    """Opens a dialog to select a directory and returns its path."""
    root = tk.Tk()
    root.withdraw()
    directory_path = filedialog.askdirectory(title=title)
    root.destroy()
    return directory_path

def select_save_file(title="Save File As", defaultextension=".json", filetypes=[("JSON files", "*.json"), ("All files", "*.*")]):
    """Opens a dialog to select a save file location and returns its path."""
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.asksaveasfilename(title=title, defaultextension=defaultextension, filetypes=filetypes)
    root.destroy()
    return file_path

def select_open_file(title="Select File", filetypes=[("JSON files", "*.json"), ("All files", "*.*")]):
    """Opens a dialog to select an existing file and returns its path."""
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.askopenfilename(title=title, filetypes=filetypes)
    root.destroy()
    return file_path

# --- Function 1: Export Folder Structure and File Metadata ---
def export_structure_to_json(test_root_dir=None, test_output_json_path=None):
    logger.info(f"Starting structure export process. Test mode: {bool(test_root_dir)}")

    root_dir = test_root_dir
    if not root_dir:
        root_dir = select_directory(title="Select the Root Folder to Scan")
        if not root_dir:
            print("Export cancelled: No root folder selected.")
            logger.warning("Export process cancelled by user: No root folder selected.")
            return

    output_json_path = test_output_json_path
    if not output_json_path:
        output_json_path = select_save_file(title="Save Structure File As", defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if not output_json_path:
            print("Export cancelled: No output file selected.")
            logger.warning("Export process cancelled by user: No output file selected for structure.")
            return

    structure_list = []
    try:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            for dirname in dirnames:
                full_path = os.path.join(dirpath, dirname)
                relative_path = os.path.relpath(full_path, root_dir)
                relative_path = relative_path.replace(os.sep, "/")
                if relative_path == ".": continue
                structure_list.append({
                    "original_relative_path": relative_path,
                    "type": "directory",
                    "name": dirname
                })

            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                relative_path = os.path.relpath(full_path, root_dir)
                relative_path = relative_path.replace(os.sep, "/")
                try:
                    size_bytes = os.path.getsize(full_path)
                    structure_list.append({
                        "original_relative_path": relative_path,
                        "type": "file",
                        "name": filename,
                        "size_bytes": size_bytes
                    })
                except OSError as e:
                    logger.error(f"Could not get size for file 	'{full_path}	': {e}")
                    print(f"Error: Could not get size for file 	'{full_path}	'. Skipping. See log for details.")
        
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(structure_list, f, indent=4, ensure_ascii=False)
        
        print(f"Successfully exported structure of 	'{root_dir}	' to 	'{output_json_path}	'")
        logger.info(f"Successfully exported structure of 	'{root_dir}	' to 	'{output_json_path}	'")

    except Exception as e:
        logger.critical(f"An unexpected error occurred during structure export: {e}", exc_info=True)
        print(f"An critical error occurred: {e}. See log for details.")

# --- Function 2: Restore Files and Structure from JSON ---
def restore_from_json(test_structure_file_path=None, test_source_files_dir=None, test_destination_root_dir=None):
    logger.info(f"Starting restore process. Test mode: {bool(test_structure_file_path)}")

    structure_file_path = test_structure_file_path
    if not structure_file_path:
        structure_file_path = select_open_file(title="Select the Structure JSON File")
        if not structure_file_path:
            print("Restore cancelled: No structure file selected.")
            logger.warning("Restore process cancelled by user: No structure file selected.")
            return

    source_files_dir = test_source_files_dir
    if not source_files_dir:
        source_files_dir = select_directory(title="Select the Source Directory (containing unorganized files)")
        if not source_files_dir:
            print("Restore cancelled: No source directory selected.")
            logger.warning("Restore process cancelled by user: No source directory selected.")
            return

    destination_root_dir = test_destination_root_dir
    if not destination_root_dir:
        destination_root_dir = select_directory(title="Select the Destination Directory (to restore structure)")
        if not destination_root_dir:
            print("Restore cancelled: No destination directory selected.")
            logger.warning("Restore process cancelled by user: No destination directory selected.")
            return

    try:
        with open(structure_file_path, "r", encoding="utf-8") as f:
            structure_data = json.load(f)
    except FileNotFoundError:
        logger.error(f"Structure file not found: {structure_file_path}")
        print(f"Error: Structure file 	'{structure_file_path}	' not found. Aborting.")
        return
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON in structure file: {structure_file_path}")
        print(f"Error: Could not decode JSON from 	'{structure_file_path}	'. Aborting.")
        return
    except Exception as e:
        logger.critical(f"Failed to read or parse structure file 	'{structure_file_path}	': {e}", exc_info=True)
        print(f"Critical error reading structure file: {e}. See log. Aborting.")
        return

    source_file_map = {}
    logger.info(f"Scanning source directory: {source_files_dir}")
    for dirpath, _, filenames in os.walk(source_files_dir):
        for filename in filenames:
            source_file_full_path = os.path.join(dirpath, filename)
            try:
                size_bytes = os.path.getsize(source_file_full_path)
                if size_bytes not in source_file_map:
                    source_file_map[size_bytes] = []
                source_file_map[size_bytes].append(source_file_full_path)
            except OSError as e:
                logger.warning(f"Could not get size for source file 	'{source_file_full_path}	': {e}. Skipping this file.")
    logger.info(f"Found {sum(len(paths) for paths in source_file_map.values())} files in source directory, mapped by size.")

    restored_source_files = set()

    for item in structure_data:
        if item["type"] == "directory":
            target_dir_path = os.path.join(destination_root_dir, item["original_relative_path"].replace("/", os.sep))
            if not os.path.exists(target_dir_path):
                try:
                    os.makedirs(target_dir_path)
                    logger.info(f"Created directory: {target_dir_path}")
                except OSError as e:
                    logger.error(f"Could not create directory 	'{target_dir_path}	': {e}")
                    print(f"Error creating directory 	'{target_dir_path}	'. See log.")

    for item in structure_data:
        if item["type"] == "file":
            original_rel_path = item["original_relative_path"].replace("/", os.sep)
            target_file_path = os.path.join(destination_root_dir, original_rel_path)
            file_size_needed = item["size_bytes"]
            original_name = item["name"]

            target_file_parent_dir = os.path.dirname(target_file_path)
            if not os.path.exists(target_file_parent_dir):
                try:
                    os.makedirs(target_file_parent_dir, exist_ok=True)
                    logger.info(f"Created missing parent directory for file: {target_file_parent_dir}")
                except OSError as e:
                    logger.error(f"Could not create parent directory 	'{target_file_parent_dir}	' for file 	'{original_name}	': {e}")
                    print(f"Error creating parent directory for 	'{original_name}	'. See log.")
                    continue

            if os.path.exists(target_file_path):
                logger.warning(f"Conflict: File 	'{target_file_path}	' already exists in destination. Skipping restoration for this file.")
                print(f"Conflict: 	'{target_file_path}	' already exists. Skipping.")
                continue

            found_source_candidates = source_file_map.get(file_size_needed, [])
            source_file_to_copy = None

            if not found_source_candidates:
                log_rel_path = original_rel_path.replace(os.sep, "/")
                logger.error(f"File not found in source: Name 	'{original_name}'	 (rel path: 	'{log_rel_path}'	), Size: {file_size_needed} bytes. Cannot restore.")
                print(f"Error: File 	'{original_name}'	 (size: {file_size_needed} bytes) not found in source. Skipping.")
                continue
            for candidate_path in found_source_candidates:
                if candidate_path not in restored_source_files:
                    source_file_to_copy = candidate_path
                    break
            
            if not source_file_to_copy:
                logger.warning(f"Conflict: All source files of size {file_size_needed} bytes (for target 	'{original_name}	') have already been used for other restorations. Skipping this file.")
                print(f"Conflict: No unused source file of size {file_size_needed} for 	'{original_name}	'. Skipping.")
                if len(found_source_candidates) > 0:
                     logger.warning(f"Details: Candidates were: {found_source_candidates}, Used: {restored_source_files}")
                continue

            if len(found_source_candidates) > 1 and source_file_to_copy in found_source_candidates:
                other_available_candidates = [p for p in found_source_candidates if p != source_file_to_copy and p not in restored_source_files]
                if other_available_candidates:
                    logger.warning(f"Conflict/Ambiguity: Multiple source files of size {file_size_needed} bytes found for target 	'{original_name}	'. Used 	'{source_file_to_copy}	'. Other available candidates: {other_available_candidates}")
                elif len(found_source_candidates) > 1:
                     logger.info(f"Multiple source files of size {file_size_needed} bytes found for target 	'{original_name}	'. Used 	'{source_file_to_copy}	'. All other candidates of this size were already used.")

            try:
                shutil.copy2(source_file_to_copy, target_file_path)
                restored_source_files.add(source_file_to_copy)
                logger.info(f"Restored: Copied 	'{source_file_to_copy}	' to 	'{target_file_path}	' (Original name: 	'{original_name}	')")
                print(f"Restored: 	'{original_name}	' to 	'{target_file_path}	'")
            except Exception as e:
                logger.error(f"Failed to copy 	'{source_file_to_copy}	' to 	'{target_file_path}	': {e}", exc_info=True)
                print(f"Error copying file 	'{original_name}	'. See log.")

    logger.info("Checking destination directory for files/folders not in the restore structure...")
    structure_relative_paths = {item["original_relative_path"].replace("/", os.sep) for item in structure_data}
    for dirpath, dirnames, filenames in os.walk(destination_root_dir):
        all_items_in_walk = list(dirnames) + list(filenames)
        for item_name in all_items_in_walk:
            current_item_full_path = os.path.join(dirpath, item_name)
            current_item_relative_path = os.path.relpath(current_item_full_path, destination_root_dir)
            if current_item_relative_path == ".": continue
            
            if current_item_relative_path not in structure_relative_paths:
                item_type = "directory" if os.path.isdir(current_item_full_path) else "file"
                logger.info(f"Unmanaged item in destination: {item_type} 	'{current_item_full_path}	' exists in destination but not in structure. Left untouched.")

    print(f"Restore process finished. Check 	'{destination_root_dir}	' and 'backup_tool.log' for details.")
    logger.info("Restore process finished.")

# --- Main application logic (simple CLI or GUI chooser) ---
def main_user_interaction():
    # Ensure logger is set to WARNING for user-facing operation as requested
    global logger
    for handler in logger.handlers[:]: # Clear test handlers if any
        logger.removeHandler(handler)
    logger.setLevel(logging.WARNING)
    fh = logging.FileHandler("backup_tool.log", encoding="utf-8", mode='a')
    fh.setLevel(logging.WARNING)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    root = tk.Tk()
    root.withdraw()
    action = simpledialog.askstring("Choose Action", "Enter '1' to Export structure, '2' to Restore from structure:", parent=root)
    if root.winfo_exists(): root.destroy()

    if action == "1":
        export_structure_to_json()
    elif action == "2":
        restore_from_json()
    else:
        if action is not None:
            print("Invalid action selected. Exiting.")
            logger.warning(f"Invalid action selected by user: '{action}'.")
        else:
            print("No action selected. Exiting.")
            logger.info("User cancelled action selection dialog.") # Info level for this event is fine

if __name__ == "__main__":
    def run_tests(): # Test function kept for reference, but not called by default
        print("--- RUNNING AUTOMATED TESTS ---")
        global logger
        # Re-initialize logger for a fresh log file for tests, set to INFO for test verbosity
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        logger.setLevel(logging.INFO)
        fh_test = logging.FileHandler("backup_tool_test_run.log", encoding="utf-8", mode='w')
        fh_test.setLevel(logging.INFO)
        formatter_test = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        fh_test.setFormatter(formatter_test)
        logger.addHandler(fh_test)
        logger.info("--- Automated Test Run Started ---")

        test_export_source_dir = "/home/ubuntu/test_export_root"
        test_exported_json_path = "/home/ubuntu/test_structure.json"
        
        test_restore_source_unorganized_dir = "/home/ubuntu/test_restore_source_files"
        test_restore_destination_dir_empty = "/home/ubuntu/test_restore_destination_empty"
        test_restore_destination_dir_conflict = "/home/ubuntu/test_restore_destination_conflict"

        for path in [test_exported_json_path, test_restore_source_unorganized_dir, 
                     test_restore_destination_dir_empty, test_restore_destination_dir_conflict, "backup_tool_test_run.log"]:
            if os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        
        os.makedirs(test_restore_source_unorganized_dir, exist_ok=True)
        os.makedirs(test_restore_destination_dir_empty, exist_ok=True)
        os.makedirs(test_restore_destination_dir_conflict, exist_ok=True)
        # Recreate test_export_root as it's cleaned up if it exists as a dir
        os.makedirs(os.path.join(test_export_source_dir, "folder1", "subfolder1"), exist_ok=True)
        os.makedirs(os.path.join(test_export_source_dir, "empty_folder"), exist_ok=True)
        os.makedirs(os.path.join(test_export_source_dir, "folder2"), exist_ok=True)
        with open(os.path.join(test_export_source_dir, "file_A.txt"), "w") as f: f.write("0123456789") #10 bytes + 1 for newline if echo did that, os.path.getsize will be accurate
        with open(os.path.join(test_export_source_dir, "folder1", "file_B.dat"), "w") as f: f.write("01234567890123456789") #20 bytes
        with open(os.path.join(test_export_source_dir, "folder1", "subfolder1", "file_C.tmp"), "w") as f: f.write("012345678901234567890123456789") #30 bytes
        with open(os.path.join(test_export_source_dir, "folder2", "file_D.log"), "w") as f: f.write("0123456789012345678901234567890123456789") #40 bytes
        with open(os.path.join(test_export_source_dir, "folder2", "file_E with spaces.txt"), "w") as f: f.write("01234567890123456789012345678901234567890123456789") #50 bytes
        with open(os.path.join(test_export_source_dir, "folder2", "another file.empty"), "w") as f: f.write("") #0 bytes

        print(f"--- Test 1: Exporting structure from {test_export_source_dir} ---")
        export_structure_to_json(test_root_dir=test_export_source_dir, test_output_json_path=test_exported_json_path)
        print(f"Export test finished. Check {test_exported_json_path} and backup_tool_test_run.log")

        shutil.copy2(os.path.join(test_export_source_dir, "file_A.txt"), os.path.join(test_restore_source_unorganized_dir, "source_file_A_renamed.txt"))
        shutil.copy2(os.path.join(test_export_source_dir, "folder1", "file_B.dat"), os.path.join(test_restore_source_unorganized_dir, "source_file_B.dat"))
        # file_C.tmp (30 bytes) is intentionally NOT copied to source_unorganized_dir to test missing file scenario
        shutil.copy2(os.path.join(test_export_source_dir, "folder2", "file_D.log"), os.path.join(test_restore_source_unorganized_dir, "source_file_D_original_name.log"))
        with open(os.path.join(test_restore_source_unorganized_dir, "duplicate_size_for_D.txt"), "wb") as f:
            f.write(os.urandom(os.path.getsize(os.path.join(test_export_source_dir, "folder2", "file_D.log")))) # Create file with same size as file_D.log
        shutil.copy2(os.path.join(test_export_source_dir, "folder2", "file_E with spaces.txt"), os.path.join(test_restore_source_unorganized_dir, "source_file_E.txt"))
        shutil.copy2(os.path.join(test_export_source_dir, "folder2", "another file.empty"), os.path.join(test_restore_source_unorganized_dir, "empty_source.empty"))

        print(f"--- Test 2: Restoring to empty directory {test_restore_destination_dir_empty} ---")
        restore_from_json(
            test_structure_file_path=test_exported_json_path,
            test_source_files_dir=test_restore_source_unorganized_dir,
            test_destination_root_dir=test_restore_destination_dir_empty
        )
        print(f"Restore to empty dir test finished. Check {test_restore_destination_dir_empty} and backup_tool_test_run.log")

        os.makedirs(os.path.join(test_restore_destination_dir_conflict, "folder1", "subfolder1"), exist_ok=True)
        with open(os.path.join(test_restore_destination_dir_conflict, "folder1", "file_B.dat"), "w") as f:
            f.write("this is a pre-existing conflicting file for file_B.dat")
        with open(os.path.join(test_restore_destination_dir_conflict, "extra_file_not_in_structure.txt"), "w") as f:
            f.write("this file should remain untouched and be logged")
        os.makedirs(os.path.join(test_restore_destination_dir_conflict, "extra_folder_not_in_structure"), exist_ok=True)
        with open(os.path.join(test_restore_destination_dir_conflict, "extra_folder_not_in_structure", "another_extra.txt"), "w") as f:
            f.write("another extra")

        print(f"--- Test 3: Restoring to conflicting directory {test_restore_destination_dir_conflict} ---")
        restore_from_json(
            test_structure_file_path=test_exported_json_path,
            test_source_files_dir=test_restore_source_unorganized_dir, 
            test_destination_root_dir=test_restore_destination_dir_conflict
        )
        print(f"Restore to conflicting dir test finished. Check {test_restore_destination_dir_conflict} and backup_tool_test_run.log")
        
        logger.info("--- Automated Test Run Finished ---")
        print("--- AUTOMATED TESTS FINISHED ---")
        print("To run with UI, comment out run_tests() and uncomment main_user_interaction() below.")

    # --- CHOOSE EXECUTION MODE ---
    # run_tests() # Commented out for final delivery
    main_user_interaction() 

    print("Backup tool script finished.")

