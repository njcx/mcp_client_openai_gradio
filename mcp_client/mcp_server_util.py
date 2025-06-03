async def verify_uvx_installation():
    """Verify that uvx is properly installed and accessible."""
    try:
        import subprocess
        result = subprocess.run(['uvx', '--version'],
                              capture_output=True,
                              text=True)
        print(f"UVX version check output: {result.stdout}")
        return True
    except FileNotFoundError:
        print("UVX not found in PATH")
        return False
    except Exception as e:
        print(f"Error checking UVX: {str(e)}")
        return False

async def verify_npx_installation():
    """Verify that npx is properly installed and accessible."""
    import subprocess
    import platform

    # Determine the command based on the operating system
    if platform.system() == 'Windows':
        cmd = 'npx.cmd'  # Windows uses .cmd extension for batch scripts
    else:
        cmd = 'npx'

    try:
        result = subprocess.run([cmd, '--version'],
                                capture_output=True,
                                text=True)
        if result.returncode == 0:
            version_output = result.stdout.strip() or result.stderr.strip()
            print(f"NPX version check output: {version_output}")
            return True
        else:
            print(f"Failed to get NPX version: {result.stderr.strip()}")
            return False
    except FileNotFoundError:
        print(f"{cmd} not found in PATH")
        return False
    except Exception as e:
        print(f"Error checking NPX: {str(e)}")
        return False

async def verify_python_installation():
    """Verify that Python is properly installed and accessible as 'python' or 'python3'."""
    import subprocess

    commands = ['python', 'python3']
    for cmd in commands:
        try:
            result = subprocess.run([cmd, '--version'],
                                    capture_output=True,
                                    text=True)
            version_output = result.stdout.strip() or result.stderr.strip()
            if result.returncode == 0:
                print(f"{cmd} version check output: {version_output}")
                return True
            else:
                print(f"Failed to get version from {cmd}: {version_output}")
        except FileNotFoundError:
            print(f"{cmd} not found in PATH")
        except Exception as e:
            print(f"Error checking {cmd}: {str(e)}")
    return False